"use strict";
(() => {
  // No browser storage, URL tokens, unsafe HTML injection or fake data.
  const MODULES=["sales","purchases","inventory","returns","refunds"];
  const labels={
    ar:{modules:"الوحدات التشغيلية",sales:"المبيعات",purchases:"المشتريات",inventory:"المخزون",
      returns:"المرتجعات",refunds:"رد أموال العملاء",experimental:"بيئة تطوير · قراءة فقط",
      readonly:"للقراءة فقط",connecttitle:"الاتصال بالشركة",
      connectdesc:"رمز الوصول يجب أن يكون موقّعًا من موفر هوية موثوق. لا يُحفظ في المتصفح.",
      tenant:"معرف الشركة",access:"رمز الوصول",connect:"الاتصال الآمن",disconnect:"إنهاء الاتصال",
      tokenpolicy:"الرمز في ذاكرة الصفحة فقط. هذا ليس نظام تسجيل دخول إنتاجيًا.",
      company:"الشركة",total:"السجلات",data:"المصدر",live:"PostgreSQL · قراءة آمنة",
      source:"بيانات الشركة المصرح بها فقط، بدون أرقام افتراضية.",refresh:"تحديث",
      previous:"السابق",next:"التالي",footer:"نظام مستقل قيد التطوير، لا ينفذ تحويلات بنكية أو إصدار ZATCA.",
      sidenote:"عزل الشركات وفحص العضوية داخل الخادم. العرض للقراءة والمراجعة.",
      waiting:"أدخل بيانات اتصال مصرحًا بها لعرض معلومات الشركة.",
      loading:"جاري قراءة السجلات المصرح بها…",empty:"لا توجد سجلات في هذه الصفحة.",
      error:"تعذر قراءة البيانات أو انتهت صلاحية الاتصال. راجع التفويض.",page:"صفحة",
      bank:"مراجع الخصم البنكي غير موثقة. عدم ظهور تأكيد سداد لا يثبت عدم انتقال المال.",
      fields:{id:"المرجع",number:"الرقم",partner_id:"العميل / المورد",
        status:"الحالة",currency:"العملة",line_count:"البنود",ordered_qty:"الكمية",
        fulfilled_qty:"المُنفّذ",order_value:"القيمة",warehouse_id:"مستودع",
        warehouse:"المستودع",product_id:"الصنف",sku:"كود الصنف",product:"المنتج",
        on_hand:"الرصيد",reserved:"المحجوز",available:"المتاح",valued_qty:"كمية التقييم",
        book_value:"القيمة الدفترية",return_kind:"نوع المرتجع",quantity:"الكمية",
        accepted_qty:"المقبول",reason:"السبب",source_event_id:"حركة المصدر",
        processed_event_id:"حركة المرتجع",requested_at:"وقت الطلب",
        destination_warehouse_id:"مخزن الوجهة",return_job_id:"المرتجع",
        original_invoice_id:"الفاتورة الأصلية",gross_return_amount:"المردود",
        ar_reduction:"تخفيض العملاء",refund_liability:"المستحق للرد",
        order_id:"أمر الصرف",approval_status:"الاعتماد",approved_at:"وقت الاعتماد",
        bank_reference_id:"مرجع بنك غير موثق",bank_reference_status:"حالة المرجع"}},
    en:{modules:"Operations",sales:"Sales",purchases:"Purchases",inventory:"Inventory",
      returns:"Returns",refunds:"Customer refunds",experimental:"Developer preview · read only",
      readonly:"Read-only",connecttitle:"Connect to company",
      connectdesc:"Requires a trusted IdP-signed access token. Never persisted in your browser.",
      tenant:"Company ID",access:"Access token",connect:"Connect",disconnect:"Disconnect",
      tokenpolicy:"Token stays only in this page's memory; this is not a production login.",
      company:"Company",total:"Records",data:"Source",live:"PostgreSQL · secure read",
      source:"Authorized company data only; no fictional amounts.",refresh:"Refresh",
      previous:"Previous",next:"Next",footer:"Independent prototype: no actual bank transfer or ZATCA issuance.",
      sidenote:"Company scoping and database membership are rechecked server-side.",
      waiting:"Enter authorized access to view company data.",loading:"Loading authorized records…",
      empty:"No records on this page.",error:"Cannot load data or session expired. Check authorization.",
      page:"Page",bank:"Bank debit references are unverified. Lack of payment evidence cannot prove no external transfer occurred.",
      fields:{id:"Reference",number:"Number",partner_id:"Customer / supplier",status:"Status",
        currency:"Currency",line_count:"Lines",ordered_qty:"Ordered",fulfilled_qty:"Fulfilled",
        order_value:"Value",warehouse_id:"Warehouse ID",warehouse:"Warehouse",
        product_id:"Product ID",sku:"SKU",product:"Product",on_hand:"On hand",
        reserved:"Reserved",available:"Available",valued_qty:"Valued qty",
        book_value:"Book value",return_kind:"Return type",quantity:"Quantity",
        accepted_qty:"Accepted",reason:"Reason",source_event_id:"Source event",
        processed_event_id:"Processed event",requested_at:"Requested",
        destination_warehouse_id:"Destination",return_job_id:"Return ID",
        original_invoice_id:"Original invoice",gross_return_amount:"Returned amount",
        ar_reduction:"AR reduction",refund_liability:"Refund payable",
        order_id:"Approval order",approval_status:"Approval",approved_at:"Approved",
        bank_reference_id:"Unverified bank reference",
        bank_reference_status:"Bank reference status"}}
  };
  let locale="ar", token="",tenant="",page=0,seq=0;
  const el=id=>document.getElementById(id);
  const moduleFromPath=()=> {
    const p=location.pathname.split("/").filter(Boolean);
    return p.length===2&&p[0]==="app"&&MODULES.includes(p[1])?p[1]:"sales";
  };
  let module=moduleFromPath();
  const tr=k=>labels[locale][k]||k;
  const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
  function translate(){
    document.documentElement.lang=locale;
    document.documentElement.dir=locale==="ar"?"rtl":"ltr";
    document.querySelectorAll("[data-i18n]").forEach(node=>{
      node.textContent=tr(node.dataset.i18n);
    });
    el("language").textContent=locale==="ar"?"English":"عربي";
    el("page-title").textContent=tr(module);
    el("crumb-module").textContent=tr(module);
    el("section-title").textContent=tr(module);
    el("page-description").textContent=tr("source");
    document.querySelectorAll("#module-nav a").forEach(a=>{
      const active=a.dataset.module===module;
      a.classList.toggle("active",active);
      if(active)a.setAttribute("aria-current","page");
      else a.removeAttribute("aria-current");
    });
    el("bank-disclaimer").hidden=module!=="refunds";
    el("bank-disclaimer").textContent=tr("bank");
    if(!token)el("status").textContent=tr("waiting");
  }
  const status=(message,isError=false)=>{
    const node=el("status");
    node.className="status "+(isError?"error":"");
    node.textContent=message;
  };
  function clearTable(){
    el("data-table").querySelector("thead").replaceChildren();
    el("data-table").querySelector("tbody").replaceChildren();
    el("total-count").textContent="—";
    el("page-index").textContent="—";
    el("next").disabled=true;el("previous").disabled=true;
  }
  function render(payload){
    // Data is always inserted as TEXT, never HTML; server controls field order.
    if(payload.module!==module||payload.tenant_id!==tenant||
       !Array.isArray(payload.fields)||!Array.isArray(payload.items)||
       payload.items.length>25)throw Error("Unexpected response");
    const thead=el("data-table").querySelector("thead");
    const tbody=el("data-table").querySelector("tbody");
    thead.replaceChildren();tbody.replaceChildren();
    const head=document.createElement("tr");
    payload.fields.forEach(field=>{
      const cell=document.createElement("th");
      cell.scope="col";cell.textContent=labels[locale].fields[field]||field;
      head.append(cell);
    });
    thead.append(head);
    payload.items.forEach(item=>{
      const row=document.createElement("tr");
      payload.fields.forEach(field=>{
        const cell=document.createElement("td"), v=item[field];
        cell.textContent=v==null?"—":String(v);
        cell.title=cell.textContent;
        row.append(cell);
      });
      tbody.append(row);
    });
    el("company-name").textContent=payload.company;
    el("total-count").textContent=String(payload.total);
    el("page-index").textContent=tr("page")+" "+String(page+1);
    el("previous").disabled=page===0;
    el("next").disabled=!payload.has_more;
    status(payload.items.length?"":tr("empty"));
  }
  async function load(){
    if(!token||!uuid.test(tenant)){status(tr("waiting"));return;}
    const current=++seq, expected=module;
    status(tr("loading"));
    try{
      const url="/api/v1/operations/"+encodeURIComponent(expected)+
          "?tenant="+encodeURIComponent(tenant)+"&page="+String(page);
      const response=await fetch(url,{
        method:"GET",mode:"same-origin",cache:"no-store",
        credentials:"omit",headers:{"Authorization":"Bearer "+token,
                                    "Accept":"application/json"}
      });
      if(!response.ok)throw Error("authorization or data unavailable");
      const data=await response.json();
      if(current!==seq||expected!==module)return;
      render(data);
    }catch(err){
      if(current===seq){clearTable();status(tr("error"),true);}
    }
  }
  function selectModule(next){
    if(!MODULES.includes(next))return;
    module=next;page=0;
    history.pushState({module:next},"","/app/"+next);
    translate();clearTable();void load();
  }
  document.querySelectorAll("#module-nav a").forEach(link=>{
    link.addEventListener("click",e=>{
      e.preventDefault();selectModule(link.dataset.module);
    });
  });
  el("connect-form").addEventListener("submit",e=>{
    e.preventDefault();
    const newTenant=el("tenant-id").value.trim().toLowerCase();
    const newToken=el("access-token").value.trim();
    el("access-token").value="";
    if(!uuid.test(newTenant)||newToken.split(".").length!==3){
      status(tr("error"),true);return;
    }
    tenant=newTenant;token=newToken;page=0;
    el("disconnect").hidden=false;
    clearTable();void load();
  });
  el("disconnect").addEventListener("click",()=>{
    seq++;tenant="";token="";page=0;
    el("tenant-id").value="";el("access-token").value="";
    el("company-name").textContent="—";
    el("disconnect").hidden=true;clearTable();translate();
  });
  el("refresh").addEventListener("click",()=>void load());
  el("next").addEventListener("click",()=>{if(page<100){page++;void load();}});
  el("previous").addEventListener("click",()=>{if(page>0){page--;void load();}});
  el("language").addEventListener("click",()=>{
    locale=locale==="ar"?"en":"ar";translate();
    if(token)void load();
  });
  window.addEventListener("popstate",()=>{
    module=moduleFromPath();page=0;translate();clearTable();void load();
  });
  clearTable();translate();
})();
