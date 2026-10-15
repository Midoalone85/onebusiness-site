/* ONE ERP Next fictional UI contract tests; no network or browser required. */
'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const root=path.resolve(__dirname,'..');
const js=fs.readFileSync(path.join(root,'ui.js'),'utf8');
const css=fs.readFileSync(path.join(root,'ui.css'),'utf8');
const home=fs.readFileSync(path.join(root,'index.html'),'utf8');
const workspace=fs.readFileSync(path.join(root,'workspace','index.html'),'utf8');

const moduleViews={
 accounting:['overview','journals','invoices','bills','payments','bank','accounts','tax','statements'],
 sales:['overview','quotes','orders','invoices','customers'],
 purchasing:['overview','requests','orders','bills','suppliers'],
 inventory:['overview','products','receipts','transfers','warehouses','valuation'],
 crm:['overview','leads','pipeline','activities','contacts'],
 healthcare:['overview','rooms','patients','appointments','doctors','lab','radiology','pharmacy','meals'],
 hr:['overview','employees','attendance','payroll','leaves','contracts'],
 reports:['overview','finance','sales','stock','tax','audit'],
};

function setup({page='home',module='accounting',view='overview',lang='ar'}={}){
 const listeners={},elements=new Map();
 const fakeElem=(id)=>({
    id,innerHTML:'',textContent:'',value:'',hidden:false,style:{},
    offsetWidth:310,
    classList:{ add(){},remove(){} },
    appendChild(){},remove(){},setAttribute(){},
    addEventListener(type,fn){(listeners['element:'+id+':'+type]??=[]).push(fn)},
 });
 const el=id=>elements.get(id)||((elements.set(id,fakeElem(id))),elements.get(id));
 const document={
   title:'',referrer:'',documentElement:{lang:'',dir:''},
   body:{dataset:{page},appendChild(){}},
   getElementById:el,
   querySelectorAll(){return []},
   createElement:(name)=>fakeElem(name),
   addEventListener(type,fn){(listeners[type]??=[]).push(fn)},
 };
 const window={innerWidth:1360,innerHeight:790,scrollY:0,
   addEventListener(type,fn){(listeners['window:'+type]??=[]).push(fn)},
   print(){window.printed=true}};
 const location={search:page==='workspace'?'?module='+module+'&view='+view:'',
   origin:'https://example.test',
   pathname:page==='workspace'?'/one-erp-preview/workspace/':'/one-erp-preview/',
   reload(){location.reloaded=true}};
 const localStorage={getItem(){return lang},setItem(_key,x){localStorage.updated=x}};
 const ctx={window,document,location,localStorage,history:{back(){window.wentBack=true}},
    URL,URLSearchParams,Intl,Blob,setTimeout,console};
 vm.runInNewContext(js,ctx,{filename:'ui.js',timeout:4000});
 return {document,window,location,elements,listeners,el,main:el('app').innerHTML,
   emit(type,target){(listeners[type]||[]).forEach(fn=>fn({target,key:''}))},
 };
}

test('static shell serves a different HTML document for every workspace',()=>{
 assert.match(home,/data-page="home"/);
 assert.match(workspace,/data-page="workspace"/);
 assert.match(home,/src="\.\/ui\.js"/);
 assert.match(workspace,/src="\.\.\/ui\.js"/);
 assert.match(home,/default-src 'none'/);
 assert.match(workspace,/connect-src 'none'/);
 assert.doesNotMatch(js,/\bfetch\(/);
 assert.doesNotMatch(js,/one-erp\.141/);
 assert.match(css,/\.flyout\{/);
 assert.match(css,/\.context-strip\{/);
});

test('every module page renders top modules, own page, back and contextual links in both languages',()=>{
 let cases=0;
 for(const lang of ['ar','en']){
  for(const [module,views] of Object.entries(moduleViews)){
   for(const view of views){
    const r=setup({page:'workspace',module,view,lang});
    assert.match(r.main,/class="navrail"/);
    assert.match(r.main,/id="backBtn"/);
    assert.match(r.main,/class="context-strip"/);
    assert.ok(r.main.includes('module='+module+'&view='+view));
    assert.equal((r.main.match(/class="context-item /g)||[]).length,views.length);
    assert.match(r.main,/DEMO DATA/);
    assert.match(r.main,/id="dataRows"/);
    assert.equal(r.document.documentElement.dir,lang==='ar'?'rtl':'ltr');
    assert.match(r.document.title,/ONE ERP Next Preview/);
    assert.doesNotMatch(r.main,/Midoalone85/);
    cases++;
   }
  }
 }
 assert.equal(cases,102);
});

test('homepage shows all eight horizontal modules and navigable links',()=>{
 for(const lang of ['ar','en']){
  const r=setup({lang});
  assert.match(r.main,/data-menu-trigger="accounting"/);
  assert.match(r.main,/class="appgrid"/);
  assert.match(r.main,/class="navrail"/);
  for(const module of Object.keys(moduleViews)){
   assert.ok(r.main.includes('module='+module+'&view=overview'));
  }
 }
});

test('accounting dropdown is a vertical list of all operations',()=>{
 const r=setup();
 const trigger={dataset:{menuTrigger:'accounting'},
  getBoundingClientRect(){return {left:110,right:275,bottom:126}},
  setAttribute(){}};
 const target={closest(sel){
   if(sel==='[data-menu-trigger]')return trigger;
   return null;
 }};
 r.emit('click',target);
 const menu=r.el('flyout');
 assert.match(menu.innerHTML,/role="menuitem"/);
 assert.equal((menu.innerHTML.match(/role="menuitem"/g)||[]).length,9);
 assert.match(menu.innerHTML,/view=journals/);
 assert.match(menu.innerHTML,/view=invoices/);
 assert.match(menu.innerHTML,/view=statements/);
 assert.match(menu.style.left,/px$/);
});

test('finance statements render amounts rather than escaped HTML',()=>{
 const r=setup({page:'workspace',module:'accounting',view:'statements'});
 assert.ok(r.main.includes('3195000')||r.main.includes('3,195,000')||r.main.includes('٣٬١٩٥٬٠٠٠'));
 assert.doesNotMatch(r.main,/&lt;span class=&quot;ltr&quot;&gt;/);
});

test('language switch preserves route and does not modify business data',()=>{
 const r=setup({page:'workspace',module:'accounting',view:'invoices'});
 r.emit('click',{closest(sel){return sel==='[data-action]'?{dataset:{action:'language'}}:null}});
 assert.equal(r.location.reloaded,true);
 assert.equal(r.document.documentElement.lang,'ar');
});
