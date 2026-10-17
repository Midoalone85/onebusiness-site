import test from "node:test";
import assert from "node:assert/strict";
import {mountOperationsPage} from "./operations-page.mjs";
class Element {
  constructor(ownerDocument,tag){this.ownerDocument=ownerDocument;this.tagName=tag;this.children=[];this.attributes={};this.textContent="";this.listeners={};}
  setAttribute(k,v){this.attributes[k]=v;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(type,callback){this.listeners[type]=callback;}
}
const document={createElement(tag){return new Element(document,tag)}};
const response=data=>({ok:true,status:200,headers:{get:()=>"application/json"},json:async()=>data});
test("renders trusted API rows as text-only Arabic RTL",async()=>{
  const root=document.createElement("main");
  const page=mountOperationsPage({root,language:"ar",baseUrl:"https://erp.example.test/",
    getToken:async()=> "a.b.c",fetchImpl:async()=>response({rows:[{customer:"<img src=x onerror=alert(1)>",total:"100"}]})});
  await page.load("sales");
  assert.equal(root.attributes.dir,"rtl");
  assert.equal(root.children[3].children[0].tagName,"table");
  assert.equal(root.children[3].children[0].children[1].children[0].children[0].textContent,"<img src=x onerror=alert(1)>");
  page.dispose();
  assert.equal(root.children.length,0);
});
test("English navigation has four safe read-only modules",()=>{
  const root=document.createElement("main");
  const page=mountOperationsPage({root,language:"en",baseUrl:"https://erp.example.test/",getToken:async()=> "a.b.c"});
  assert.equal(root.attributes.dir,"ltr");
  assert.deepEqual(root.children[0].children.map(el=>el.textContent),["Sales","Purchases","Inventory","Returns"]);
  page.dispose();
});
test("login denial cannot display stale tenant rows",async()=>{
  let count=0;
  const root=document.createElement("main");
  const page=mountOperationsPage({root,baseUrl:"https://erp.example.test/",getToken:async()=> "a.b.c",
    fetchImpl:async()=>++count===1?response({rows:[{id:"secret"}]}):({ok:false,status:403})});
  await page.load("sales");
  await page.load("inventory");
  assert.equal(root.children[3].children.length,0);
  assert.equal(root.children[2].textContent,"لا تملك صلاحية الاطلاع على هذه البيانات");
  page.dispose();
});
