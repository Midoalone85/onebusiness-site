/** Additional reviewer safety and accessibility regressions.
 * This separate suite intentionally preserves the exact original PR #51
 * invoice-review-client.test.mjs Git blob for sibling-source merge audit.
 */
import test from "node:test";
import assert from "node:assert/strict";
import {validateInternalInvoiceSource} from "./invoice-review-client.mjs";
import {mountInvoiceApprovalPage} from "./invoice-approval-page.mjs";

const tenant="11111111-1111-4111-8111-111111111111";
const document_id="22222222-2222-4222-8222-222222222222";
const line={
  line_number:1,description:"Genuine original invoice source",
  quantity:"2",unit_price:"100.00",discount_amount:"10.00",
  tax_category:"standard",tax_rate:"0.150000",tax_code:"KSA_STANDARD",
  net:"190.00",tax:"28.50",gross:"218.50",
};
const source={
  tenant,document_id,reviewable:true,
  source:"verified_internal_postgresql",
  fields:[{label:"Gross",value:"218.50"}],
  lines:[line],
};

test("full genuine reviewer source still passes after strict monetary checks",()=>{
  const result=validateInternalInvoiceSource(source,{tenant,document_id});
  assert.deepEqual(result.lines,[line]);
  const noTax={...line,tax_category:"out_of_scope",tax_rate:"0",
    tax_code:"NONE",tax:"0.00",gross:"190.00"};
  assert.equal(validateInternalInvoiceSource(
    {...source,lines:[noTax]},{tenant,document_id}).reviewable,true);
});

test("fake financial evidence must not become an approvable reviewer source",()=>{
  const cases=[
    {gross:"Infinity"},{net:"NaN"},{tax:"1e309"},{quantity:"0"},
    {quantity:"-2"},{quantity:"2,000"},{unit_price:"-100.00"},
    {discount_amount:"abc"},{net:"190.00",gross:"219.50"},
    {tax_category:"invented"},{tax_category:"exempt",tax_rate:"0.150000"},
    {tax_rate:"1.25"},{tax_rate:"-0.1"},{tax_rate:"0"},
    {tax_code:""},{tax_code:"NONE"},
  ];
  for(const change of cases){
    assert.throws(
      ()=>validateInternalInvoiceSource(
        {...source,lines:[{...line,...change}]},{tenant,document_id}),
      {name:"InternalApprovalError"},JSON.stringify(change));
  }
});

class Element {
  constructor(doc,tag) {
    this.ownerDocument=doc;this.tagName=tag;this.children=[];
    this.attributes={};this.textContent="";this.listeners={};
    this.disabled=false;this.checked=false;
  }
  setAttribute(key,value){this.attributes[key]=value;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(type,callback){this.listeners[type]=callback;}
}
const document={createElement(tag){return new Element(document,tag);}};

test("review acknowledgement label is associated with its checkbox",async()=>{
  const root=document.createElement("main");
  const page=mountInvoiceApprovalPage({root,
    baseUrl:"https://erp.example.test/",
    getToken:async()=> "a.b.c",
    loadSummary:async()=>source,
  });
  assert.equal(await page.open({tenant,document_id}),true);
  const label=root.children[5],input=root.children[6];
  assert.equal(label.tagName,"label");
  assert.equal(label.attributes.for,input.attributes.id);
  assert.match(input.attributes.id,/^one-private-approval-ack-[0-9]+$/);
  page.dispose();
});

test("custom loader with mismatched invoice totals cannot enable financial approval",async()=>{
  const root=document.createElement("main");
  let requests=0;
  const page=mountInvoiceApprovalPage({root,
    baseUrl:"https://erp.example.test/",
    getToken:async()=> "a.b.c",
    loadSummary:async()=>({...source,lines:[{...line,gross:"999999.99"}]}),
    fetchImpl:async()=>{requests++;throw Error("must never call");},
  });
  assert.equal(await page.open({tenant,document_id}),false);
  assert.equal(page.snapshot().ready,false);
  const approve=root.children[7],ack=root.children[6];
  ack.checked=true;ack.listeners.change();
  assert.equal(approve.disabled,true);
  await approve.listeners.click();
  assert.equal(requests,0);
  page.dispose();
});
