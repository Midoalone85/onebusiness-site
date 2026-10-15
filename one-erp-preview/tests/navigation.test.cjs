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
 sales:['overview','quotes','orders','invoices','deliveries','proof','returns','return_case','customers'],
 purchasing:['overview','requests','orders','receiving','returns','return_case','bills','three_way','suppliers'],
 inventory:['overview','products','receipts','picking','deliveries','quality','returns','quarantine','return_case','transfers','warehouses','valuation','reconciliation'],
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
    focus(){document.activeElement=this},
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
   fire(type,event){(listeners[type]||[]).forEach(fn=>fn(event))},
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
 assert.equal(cases,132);
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


test('compact mobile hierarchy preserves data density and readable labels',()=>{
 const mobileBlock=css.slice(css.indexOf('ONE ERP COMPACT INTERFACE v2'));
 assert.ok(mobileBlock.length>1000,'mobile layout override must be present');
 assert.match(mobileBlock,/@media\(max-width:710px\)/);
 assert.match(mobileBlock,/\.kpis\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\);gap:0/);
 assert.match(mobileBlock,/\.kpi\{min-width:0;min-height:76px/);
 assert.match(mobileBlock,/\.kpi-note\{display:none\}/);
 assert.match(mobileBlock,/\.context-heading\{display:none\}/);
 assert.match(mobileBlock,/\.work-tools\{[^}]*flex-wrap:nowrap;overflow-x:auto/);
 assert.match(mobileBlock,/\.work-header\{min-height:0/);
 assert.match(mobileBlock,/tbody tr\{display:grid/);
 assert.match(mobileBlock,/td:nth-child\(4\)::before\{content:attr\(data-label\)/);
 assert.match(mobileBlock,/@media\(max-width:380px\)/);
 const invo=setup({page:'workspace',module:'accounting',view:'invoices'});
 assert.equal((invo.main.match(/class="kpi"/g)||[]).length,4);
 assert.match(invo.main,/data-label="القيمة"/);
 assert.match(invo.main,/data-label="التاريخ"/);
 assert.match(invo.main,/class="tablewrap"/);
 const en=setup({page:'workspace',module:'accounting',view:'invoices',lang:'en'});
 assert.match(en.main,/data-label="Amount"/);
 assert.match(en.main,/data-label="Date"/);
});

test('module flyout opens from arrow keys, cycles links, and returns focus on Escape',()=>{
 const r=setup({page:'workspace',module:'accounting',view:'invoices'});
 assert.match(r.main,/aria-haspopup="menu" aria-controls="flyout"/);
 const trigger={dataset:{menuTrigger:'accounting'},getBoundingClientRect(){return {left:50,right:200,bottom:120}},
   closest(sel){return sel==='[data-menu-trigger]'?this:null},
   setAttribute(){},focus(){r.document.activeElement=this}};
 const items=Array.from({length:9},(_,i)=>({
   focus(){r.document.activeElement=this},
   closest(sel){return sel==='#flyout'?r.el('flyout'):null}
 }));
 r.el('flyout').querySelectorAll=()=>items;
 const event=(key,target)=>({key,target,preventDefault(){this.prevented=true}});
 r.fire('keydown',event('ArrowDown',trigger));
 assert.equal(r.document.activeElement,items[0]);
 r.fire('keydown',event('ArrowUp',items[0]));
 assert.equal(r.document.activeElement,items[8]);
 r.fire('keydown',event('Home',items[8]));
 assert.equal(r.document.activeElement,items[0]);
 r.fire('keydown',event('End',items[0]));
 assert.equal(r.document.activeElement,items[8]);
 r.fire('keydown',event('Escape',items[8]));
 assert.equal(r.document.activeElement,trigger);
});

test('search empty result never counts placeholder as a record and keeps live count',()=>{
 const r=setup({page:'workspace',module:'accounting',view:'invoices'});
 assert.match(r.main,/id="recordCount" role="status" aria-live="polite"/);
 const tbody=r.el('dataRows'),counter=r.el('recordCount');
 const rows=[{textContent:'Invoice A',hidden:false,cells:[1,2,3,4,5]},{textContent:'Invoice B',hidden:false,cells:[1,2,3,4,5]}];
 const placeholders=[];
 tbody.appendChild=x=>{placeholders.push(x);return x};
 r.document.querySelectorAll=selector=>selector==='#dataRows tr'?rows.concat(placeholders.filter(x=>!x.removed)):[];
 r.document.getElementById=id=>{
  if(id==='noMatchRow')return placeholders.find(x=>!x.removed)||null;
  return r.el(id);
 };
 // Simulate browser tr.remove() by removing it from the current DOM result.
 r.document.createElement=tag=>({id:'',style:{},cells:[],appendChild(){},remove(){this.removed=true}});
 const fire=value=>r.fire('input',{target:{id:'recordSearch',value}});
 fire('absent');
 assert.equal(rows.every(x=>x.hidden),true);
 assert.match(counter.textContent,/0 \/ 2/);
 assert.equal(placeholders.length,1);
 fire('invoice a');
 assert.equal(rows[0].hidden,false);
 assert.equal(rows[1].hidden,true);
 assert.equal(placeholders[0].removed,true);
 assert.match(counter.textContent,/1 \/ 2/);
 fire('');
 assert.equal(rows.every(x=>!x.hidden),true);
 assert.match(counter.textContent,/2 \/ 2/);
});

test('delivery and receiving routes share stable fictional references across modules',()=>{
 for(const lang of ['ar','en']){
  const sale=setup({page:'workspace',module:'sales',view:'deliveries',lang});
  const stock=setup({page:'workspace',module:'inventory',view:'deliveries',lang});
  const receiving=setup({page:'workspace',module:'purchasing',view:'receiving',lang});
  const incoming=setup({page:'workspace',module:'inventory',view:'quality',lang});
  for(const m of [sale,stock]){
   assert.match(m.main,/DEL-2026-006/);
   assert.match(m.main,/SO-2026-024/);
   assert.match(m.main,/class="related-workflow"/);
  }
  for(const m of [receiving,incoming]){
   assert.match(m.main,/REC-2026-003/);
   assert.match(m.main,/PO-2026-011/);
   assert.match(m.main,/class="related-workflow"/);
  }
  assert.match(sale.main,/module=inventory&view=picking/);
  assert.match(stock.main,/module=sales&view=proof/);
  assert.match(receiving.main,/module=accounting&view=bills/);
  assert.match(incoming.main,/module=purchasing&view=receiving/);
  assert.match(receiving.main,/DEMO DATA/);
 }
});
test('stage-specific modules preserve separate URLs, bilingual titles and demo actions',()=>{
 for(const lang of ['ar','en']){
  for(const [module,view] of [
    ['sales','proof'],['inventory','picking'],['inventory','quality'],
    ['purchasing','receiving']
  ]){
    const r=setup({page:'workspace',module,view,lang});
    assert.ok(r.main.includes('module='+module+'&view='+view));
    assert.match(r.main,/id="backBtn"/);
    assert.match(r.main,/data-action="demo"/);
    assert.match(r.main,/id="recordSearch"/);
  }
 }
});

test('returns and quarantine show consistent immutable sample references',()=>{
 for(const lang of ['ar','en']){
   const customer=setup({page:'workspace',module:'sales',view:'returns',lang});
   const vendor=setup({page:'workspace',module:'purchasing',view:'returns',lang});
   const inventory=setup({page:'workspace',module:'inventory',view:'returns',lang});
   const quarantine=setup({page:'workspace',module:'inventory',view:'quarantine',lang});
   for(const r of [customer,inventory,quarantine]){
     assert.match(r.main,/RMA-C-2026-006/);
     assert.match(r.main,/DEL-2026-006/);
     assert.match(r.main,/DEMO DATA/);
     assert.match(r.main,/class="related-workflow"/);
   }
   assert.match(vendor.main,/RMA-V-2026-004/);
   assert.match(vendor.main,/REC-2026-003/);
   assert.match(customer.main,/module=inventory&view=quarantine/);
   assert.match(vendor.main,/module=purchasing&view=return_case/);
   assert.match(inventory.main,/module=inventory&view=return_case/);
   assert.match(quarantine.main,/module=sales&view=returns/);
   for(const r of [customer,vendor,inventory,quarantine]){
     assert.match(r.main,/id="backBtn"/);
     assert.match(r.main,/data-action="demo"/);
     assert.match(r.main,/role="status"/);
   }
 }
});


test('unified returns display separate stock and finance status across business modules',()=>{
 for(const lang of ['ar','en']){
   for(const module of ['sales','purchasing','inventory']){
     const r=setup({page:'workspace',module,view:'return_case',lang});
     assert.match(r.main,/class="return-dual-state"/);
     assert.match(r.main,/id="backBtn"/);
     assert.match(r.main,/id="recordCount"/);
     assert.match(r.main,/DEMO DATA/);
     assert.ok(r.main.includes('module='+module+'&view=return_case'));
     assert.match(r.main,/class="related-workflow"/);
     if(module==='purchasing'){
       assert.match(r.main,/RMA-V-2026-004/);
       assert.match(r.main,/accounting&view=bills/);
     }else{
       assert.match(r.main,/RMA-C-2026-006/);
       assert.match(r.main,/inventory&view=quarantine/);
       assert.match(r.main,/accounting&view=invoices/);
     }
     assert.equal(r.document.documentElement.dir,lang==='ar'?'rtl':'ltr');
   }
 }
});

test('source-backed finance stock and vendor match sample pages cross-navigate',()=>{
 for(const lang of ['ar','en']){
  const valued=setup({page:'workspace',module:'inventory',view:'reconciliation',lang});
  const stock=setup({page:'workspace',module:'inventory',view:'valuation',lang});
  const buyer=setup({page:'workspace',module:'purchasing',view:'three_way',lang});
  assert.match(valued.main,/STK-2026-015/);
  assert.match(stock.main,/COGS-2026-015/);
  assert.match(valued.main,/GRNI-2026-015/);
  assert.match(buyer.main,/PO-2026-015/);
  assert.match(buyer.main,/REC-2026-015/);
  assert.match(buyer.main,/VB-2026-015/);
  assert.match(valued.main,/module=purchasing&view=three_way/);
  assert.match(buyer.main,/module=inventory&view=reconciliation/);
  assert.match(buyer.main,/module=accounting&view=journals/);
  for(const r of [valued,stock,buyer]){
    assert.match(r.main,/DEMO DATA/);
    assert.match(r.main,/id="backBtn"/);
    assert.match(r.main,/id="recordSearch"/);
    assert.match(r.main,/class="related-workflow"/);
    assert.equal(r.document.documentElement.dir,lang==='ar'?'rtl':'ltr');
  }
 }
});
