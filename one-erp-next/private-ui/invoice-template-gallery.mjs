/*
 * Draft, private UI only. Never interpret a selected visual template as
 * legal clearance to issue an electronic invoice.
 */
export const INVOICE_TEMPLATES=Object.freeze([
  Object.freeze({id:"executive-blue",nameAr:"الأزرق التنفيذي",nameEn:"Executive Blue",className:"one-invoice-executive"}),
  Object.freeze({id:"minimal",nameAr:"المؤسسي الهادئ",nameEn:"Minimal Corporate",className:"one-invoice-minimal"}),
  Object.freeze({id:"modern",nameAr:"العصري المتوازن",nameEn:"Modern Balance",className:"one-invoice-modern"})
]);
const COUNTRY_DISPLAY_POLICIES=Object.freeze({
  SA:Object.freeze({
    countryCode:"SA",countryAr:"السعودية",countryEn:"Saudi Arabia",
    currency:"SAR",paper:"A4",orientation:"portrait",
    mandatoryDisplayLanguage:"ar",additionalDisplayLanguage:"en",
    authority:"ZATCA",policyId:"SA-INVOICE-DISPLAY-DRAFT-2026-10",
    legalStatus:"presentation_only_unverified"
  })
});
export function resolveInvoiceDisplayPolicy(countryCode){
  if(typeof countryCode!=="string"||!/^[A-Z]{2}$/.test(countryCode))
    throw new TypeError("Valid ISO-3166 alpha-2 uppercase country code required");
  const policy=COUNTRY_DISPLAY_POLICIES[countryCode];
  if(!policy)return Object.freeze({
    countryCode,status:"needs_country_review",templates:[],
    canIssue:false,reason:"Local statutory fields, language, tax and e-invoicing policy must be reviewed before enabling."
  });
  return Object.freeze({countryCode,status:"display_preview_only",policy,
    templates:INVOICE_TEMPLATES,canIssue:false});
}
const SAMPLE=Object.freeze({
  number:"DEMO-0001",date:"2026-10-18",
  sellerAr:"شركة تجريبية",sellerEn:"Demo Company",
  buyerAr:"عميل تجريبي",buyerEn:"Demo Customer",
  itemAr:"منتج تجريبي",itemEn:"Demo Product",
  quantity:"2",unitPrice:"100.00",net:"200.00",
  vatRate:"15%",vat:"30.00",total:"230.00"
});
const WORDS=Object.freeze({
  ar:{
    gallery:"قوالب الفاتورة",notice:"معاينة شكلية ببيانات وهمية فقط. ليست فاتورة ضريبية قابلة للإصدار، ولا إثبات ربط بزاتكا.",
    unavailable:"لا توجد حزمة فاتورة معتمدة للعرض لهذه الدولة. يلزم مراجعة المتطلبات المحلية أولًا.",
    title:"فاتورة ضريبية",subtitle:"Tax Invoice",preview:"معاينة تصميم تجريبي",
    invoiceNo:"رقم الفاتورة / Invoice No.",date:"التاريخ / Date",
    seller:"المورد / Supplier",buyer:"العميل / Customer",
    item:"الوصف / Description",quantity:"الكمية / Qty",
    price:"سعر الوحدة / Unit Price",net:"الصافي / Net",
    vatRate:"نسبة ضريبة القيمة المضافة / VAT Rate",
    vat:"ضريبة القيمة المضافة / VAT",total:"الإجمالي شامل VAT / Total Incl. VAT",
    currency:"العملة / Currency",qr:"رمز QR يُدرج فقط من مسار فوترة موثوق بعد التحقق"
  },
  en:{
    gallery:"Invoice templates",notice:"Fictional-data layout preview only. Not an issuable tax invoice or ZATCA clearance.",
    unavailable:"No reviewed invoice country pack is available. Local rules must be reviewed first.",
    title:"Tax Invoice",subtitle:"فاتورة ضريبية",preview:"Demo layout preview",
    invoiceNo:"Invoice No. / رقم الفاتورة",date:"Date / التاريخ",
    seller:"Supplier / المورد",buyer:"Customer / العميل",
    item:"Description / الوصف",quantity:"Qty / الكمية",
    price:"Unit Price / سعر الوحدة",net:"Net / الصافي",
    vatRate:"VAT Rate / نسبة ضريبة القيمة المضافة",
    vat:"VAT / ضريبة القيمة المضافة",total:"Total Incl. VAT / الإجمالي شامل الضريبة",
    currency:"Currency / العملة",qr:"Verified e-invoice QR is added only by an authorized issuance pipeline"
  }
});
function node(doc,tag,content,cssClass){
  const el=doc.createElement(tag);
  if(content!==undefined)el.textContent=String(content);
  if(cssClass)el.className=cssClass;
  return el;
}
function detail(doc,section,label,value){
  const box=node(doc,"div",undefined,"one-invoice-detail");
  box.append(node(doc,"span",label),node(doc,"strong",value));
  section.append(box);
}
function makePreview(doc,policy,template,words,lang){
  const article=node(doc,"article",undefined,"one-invoice-sheet "+template.className);
  article.setAttribute("aria-label",words.preview);
  article.setAttribute("dir",lang==="ar"?"rtl":"ltr");
  const top=node(doc,"header",undefined,"one-invoice-head");
  const brand=node(doc,"div",undefined,"one-invoice-brand");
  brand.append(node(doc,"strong",SAMPLE.sellerAr),node(doc,"span",SAMPLE.sellerEn));
  const heading=node(doc,"div",undefined,"one-invoice-heading");
  heading.append(node(doc,"h2",words.title),node(doc,"p",words.subtitle));
  top.append(brand,heading);
  article.append(top);
  const mark=node(doc,"p",words.preview,"one-invoice-preview-stamp");
  article.append(mark);
  const meta=node(doc,"section",undefined,"one-invoice-meta");
  detail(doc,meta,words.invoiceNo,SAMPLE.number);
  detail(doc,meta,words.date,SAMPLE.date);
  detail(doc,meta,words.currency,policy.currency);
  article.append(meta);
  const parties=node(doc,"section",undefined,"one-invoice-parties");
  const seller=node(doc,"div"),buyer=node(doc,"div");
  detail(doc,seller,words.seller,SAMPLE.sellerAr+" / "+SAMPLE.sellerEn);
  detail(doc,buyer,words.buyer,SAMPLE.buyerAr+" / "+SAMPLE.buyerEn);
  parties.append(seller,buyer);article.append(parties);
  const table=node(doc,"table");
  const thead=node(doc,"thead"),headRow=node(doc,"tr");
  const keys=["item","quantity","price","net","vatRate","vat","total"];
  for(const key of keys)headRow.append(node(doc,"th",words[key]));
  thead.append(headRow);
  const tbody=node(doc,"tbody"),row=node(doc,"tr");
  for(const value of [SAMPLE.itemAr+" / "+SAMPLE.itemEn,SAMPLE.quantity,
    SAMPLE.unitPrice,SAMPLE.net,SAMPLE.vatRate,SAMPLE.vat,SAMPLE.total])
    row.append(node(doc,"td",value));
  tbody.append(row);table.append(thead,tbody);
  article.append(table);
  const totals=node(doc,"section",undefined,"one-invoice-totals");
  detail(doc,totals,words.net,SAMPLE.net+" "+policy.currency);
  detail(doc,totals,words.vat,SAMPLE.vat+" "+policy.currency);
  detail(doc,totals,words.total,SAMPLE.total+" "+policy.currency);
  article.append(totals);
  const footer=node(doc,"footer",words.qr,"one-invoice-footer");
  article.append(footer);
  return article;
}
/*
 * Deliberately accepts NO amount payload or tenant identity. This gallery is
 * a fictional-data design picker, not an invoice issuer or taxpayer printer.
 */
export function mountInvoiceTemplateGallery({root,countryCode="SA",language="ar",initialTemplate="executive-blue"}={}){
  if(!root||!root.ownerDocument)throw new TypeError("Root element required");
  const resolved=resolveInvoiceDisplayPolicy(countryCode);
  const lang=language==="en"?"en":"ar",words=WORDS[lang],doc=root.ownerDocument;
  const heading=node(doc,"h2",words.gallery);
  const notice=node(doc,"p",words.notice,"one-invoice-warning");
  root.setAttribute("dir",lang==="ar"?"rtl":"ltr");
  root.className="one-invoice-gallery";
  if(!resolved.policy){
    const unavailable=node(doc,"p",words.unavailable);
    root.replaceChildren(heading,notice,unavailable);
    return Object.freeze({status:resolved.status,select:()=>false,dispose:()=>root.replaceChildren()});
  }
  const picker=node(doc,"nav",undefined,"one-invoice-picker");
  picker.setAttribute("aria-label",words.gallery);
  const preview=node(doc,"div",undefined,"one-invoice-preview");
  const buttons=new Map();
  let selected=INVOICE_TEMPLATES.some(t=>t.id===initialTemplate)?initialTemplate:INVOICE_TEMPLATES[0].id;
  let disposed=false;
  function render(){
    const template=INVOICE_TEMPLATES.find(t=>t.id===selected);
    for(const [id,button] of buttons)button.setAttribute("aria-pressed",String(id===selected));
    preview.replaceChildren(makePreview(doc,resolved.policy,template,words,lang));
  }
  for(const template of INVOICE_TEMPLATES){
    const button=node(doc,"button",lang==="ar"?template.nameAr:template.nameEn);
    button.type="button";button.addEventListener("click",()=>select(template.id));
    buttons.set(template.id,button);picker.append(button);
  }
  function select(templateId){
    if(disposed||!buttons.has(templateId))return false;
    selected=templateId;render();return true;
  }
  root.replaceChildren(heading,notice,picker,preview);
  render();
  return Object.freeze({
    status:resolved.status,select,selected:()=>selected,
    dispose:()=>{disposed=true;buttons.clear();root.replaceChildren();}
  });
}
