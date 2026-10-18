import test from "node:test";
import assert from "node:assert/strict";
import {INVOICE_TEMPLATES,resolveInvoiceDisplayPolicy,mountInvoiceTemplateGallery}
  from "./invoice-template-gallery.mjs";

class Element {
  constructor(doc,tag){
    this.ownerDocument=doc;this.tagName=tag;
    this.children=[];this.attributes={};this.textContent="";
    this.className="";this.listeners={};this.type="";
  }
  setAttribute(key,value){this.attributes[key]=String(value)}
  append(...children){this.children.push(...children)}
  replaceChildren(...children){this.children=children;this.textContent=""}
  addEventListener(name,fn){this.listeners[name]=fn}
}
const document={createElement(tag){return new Element(document,tag)}};
const root=()=>document.createElement("main");
const walk=element=>[element,...element.children.flatMap(walk)];
const textContent=element=>walk(element).map(x=>x.textContent).filter(Boolean).join(" ");

test("Saudi preview has three distinct user-selectable A4 templates",()=>{
  const policy=resolveInvoiceDisplayPolicy("SA");
  assert.equal(policy.status,"display_preview_only");
  assert.equal(policy.policy.paper,"A4");
  assert.equal(policy.policy.mandatoryDisplayLanguage,"ar");
  assert.equal(policy.canIssue,false);
  assert.equal(INVOICE_TEMPLATES.length,3);
  assert.equal(new Set(INVOICE_TEMPLATES.map(t=>t.className)).size,3);
  const el=root(),gallery=mountInvoiceTemplateGallery({root:el,countryCode:"SA"});
  assert.equal(el.attributes.dir,"rtl");
  assert.equal(gallery.status,"display_preview_only");
  assert.equal(gallery.selected(),"executive-blue");
  assert.equal(el.children[2].children.length,3);
  const firstPreview=el.children[3].children[0];
  assert.match(firstPreview.className,/one-invoice-executive/);
  assert.match(textContent(firstPreview),/فاتورة ضريبية/);
  assert.match(textContent(firstPreview),/Tax Invoice/);
  assert.match(textContent(firstPreview),/VAT/);
  assert.match(textContent(firstPreview),/DEMO-0001/);
  assert.match(textContent(el),/ليست فاتورة ضريبية قابلة للإصدار/);
  assert.equal(gallery.select("modern"),true);
  assert.equal(gallery.selected(),"modern");
  assert.match(el.children[3].children[0].className,/one-invoice-modern/);
  assert.equal(el.children[2].children[2].attributes["aria-pressed"],"true");
  assert.equal(gallery.select("__unknown__"),false);
  gallery.dispose();
  assert.equal(el.children.length,0);
  assert.equal(gallery.select("minimal"),false);
});
test("English preview still retains Arabic labels and Arabic/English customer metadata",()=>{
  const el=root();
  const gallery=mountInvoiceTemplateGallery({root:el,language:"en",initialTemplate:"minimal"});
  assert.equal(el.attributes.dir,"ltr");
  assert.match(el.children[3].children[0].className,/one-invoice-minimal/);
  assert.match(textContent(el),/فاتورة ضريبية/);
  assert.match(textContent(el),/ضريبة القيمة المضافة/);
  assert.match(textContent(el),/Tax Invoice/);
  assert.match(textContent(el),/Demo Company/);
  gallery.dispose();
});
test("No templates, preview data or implied issuance for countries without reviewed local policy",()=>{
  for(const cc of ["EG","AE","IN","SD"]){
    const policy=resolveInvoiceDisplayPolicy(cc);
    assert.equal(policy.status,"needs_country_review");
    assert.equal(policy.canIssue,false);
    assert.deepEqual(policy.templates,[]);
    const el=root(),gallery=mountInvoiceTemplateGallery({root:el,countryCode:cc});
    assert.equal(gallery.select("executive-blue"),false);
    assert.equal(el.children.length,3);
    assert.equal(el.children.some(x=>x.tagName==="nav"),false);
    gallery.dispose();
  }
  assert.throws(()=>resolveInvoiceDisplayPolicy("sa"),TypeError);
  assert.throws(()=>resolveInvoiceDisplayPolicy("SA<script>"),TypeError);
});
