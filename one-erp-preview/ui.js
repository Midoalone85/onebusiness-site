/* ONE ERP Next: fictional-data navigation preview, no backend and no personal data. */
(function(){
'use strict';
var PAGE=document.body.dataset.page||'home';
var root=PAGE==='workspace'?'../':'./';
var q=new URLSearchParams(location.search);
var lang;
try{lang=localStorage.getItem('one_erp_preview_lang')==='en'?'en':'ar'}catch(e){lang='ar'}
var MODS=[
{id:'accounting',ar:'المحاسبة',en:'Accounting',icon:'ledger',arSub:'الإدارة المالية والقيود والتقارير',enSub:'Financial management and reports',views:[
['overview','نظرة عامة','Overview'],['journals','القيود اليومية','Journal entries'],['invoices','فواتير العملاء','Customer invoices'],['bills','فواتير الموردين','Vendor bills'],['payments','القبض والصرف','Receipts & payments'],['bank','البنوك والتسويات','Banking & reconciliation'],['accounts','دليل الحسابات','Chart of accounts'],['tax','الضرائب والزكاة','VAT & Zakat'],['statements','القوائم المالية','Financial statements']]},
{id:'sales',ar:'المبيعات',en:'Sales',icon:'cart',arSub:'من عروض الأسعار حتى التحصيل',enSub:'Quotes, orders and billing',views:[['overview','نظرة عامة','Overview'],['quotes','عروض الأسعار','Quotations'],['orders','أوامر البيع','Sales orders'],['invoices','فواتير المبيعات','Sales invoices'],['customers','العملاء','Customers']]},
{id:'purchasing',ar:'المشتريات',en:'Purchasing',icon:'bag',arSub:'من طلب الشراء حتى فاتورة المورد',enSub:'Requisitions to vendor bills',views:[['overview','نظرة عامة','Overview'],['requests','طلبات عروض الأسعار','Requests for quotation'],['orders','أوامر الشراء','Purchase orders'],['bills','فواتير الموردين','Vendor bills'],['suppliers','الموردون','Suppliers']]},
{id:'inventory',ar:'المخزون',en:'Inventory',icon:'package',arSub:'الأصناف والمستودعات وحركة المخزون',enSub:'Products, warehouses and transfers',views:[['overview','نظرة عامة','Overview'],['products','الأصناف','Products'],['receipts','الاستلامات','Receipts'],['transfers','حركات المخزون','Transfers'],['warehouses','المستودعات','Warehouses'],['valuation','تقييم المخزون','Stock valuation']]},
{id:'crm',ar:'علاقات العملاء',en:'CRM',icon:'users',arSub:'العملاء المحتملون وفرص المبيعات',enSub:'Leads, pipeline and follow-ups',views:[['overview','نظرة عامة','Overview'],['leads','العملاء المحتملون','Leads'],['pipeline','الفرص التجارية','Pipeline'],['activities','المتابعات','Activities'],['contacts','جهات الاتصال','Contacts']]},
{id:'healthcare',ar:'القطاع الطبي',en:'Healthcare',icon:'medical',arSub:'المستشفيات والعيادات والمختبرات',enSub:'Hospitals, clinics and labs',views:[['overview','نظرة عامة','Overview'],['rooms','الغرف والأسرة','Rooms & beds'],['patients','ملفات المرضى','Patient records'],['appointments','الحجوزات والعيادات','Appointments'],['doctors','الأطباء والتمريض','Doctors & nursing'],['lab','التحاليل الطبية','Lab tests'],['radiology','الأشعة','Radiology'],['pharmacy','الصيدلية والأدوية','Pharmacy'],['meals','وجبات المرضى','Patient meals']]},
{id:'hr',ar:'الموارد البشرية',en:'Human resources',icon:'people',arSub:'الموظفون والحضور والرواتب',enSub:'People, attendance and payroll',views:[['overview','نظرة عامة','Overview'],['employees','الموظفون','Employees'],['attendance','الحضور والانصراف','Attendance'],['payroll','الرواتب ومسيرات الأجور','Payroll'],['leaves','الإجازات','Leave requests'],['contracts','العقود','Contracts']]},
{id:'reports',ar:'التقارير',en:'Reports',icon:'chart',arSub:'تقارير مالية وتشغيلية قابلة للمراجعة',enSub:'Financial and operational insights',views:[['overview','مركز التقارير','Report center'],['finance','التقارير المالية','Financial reports'],['sales','تقارير المبيعات','Sales reports'],['stock','تقارير المخزون','Inventory reports'],['tax','التقارير الضريبية','Tax reports'],['audit','سجل المراجعة','Audit trail']]}
];
var TR={
ar:{home:'لوحة البيانات',next:'ONE ERP Next',homeHeading:'لوحة الأعمال',homeDesc:'كل تطبيقات شركتك في مكان واحد، وكل عملية في مساحة عمل مستقلة مصممة لها.',demo:'معاينة ببيانات افتراضية فقط. لا يوجد تسجيل دخول أو معاملات حقيقية.',preview:'معاينة تفاعلية',fictional:'بيانات افتراضية',back:'رجوع',applications:'التطبيقات',workspace:'مساحة العمل',quick:'الصفحات المرتبطة',more:'القائمة الكاملة',latest:'آخر الأنشطة التجريبية',revenue:'الإيرادات',profit:'صافي الربح',orders:'أوامر البيع',invoices:'الفواتير',purchases:'طلبات الشراء',overdue:'متأخرة',pulse:'ONE PULSE',activity:'المراجعة والتشغيل',selected:'الصفحة الحالية',records:'سجلات توضيحية',search:'بحث في السجلات الافتراضية...',status:'الحالة',number:'المرجع',date:'التاريخ',details:'البيان',total:'القيمة',currency:'ر.س',posted:'مرحل',draft:'مسودة',pending:'قيد المراجعة',view:'عرض التفاصيل',pageTools:'أدوات الصفحة',period:'أكتوبر ٢٠٢٦',export:'تصدير CSV تجريبي',print:'طباعة',refresh:'تحديث العرض',create:'إنشاء جديد',notLive:'وظيفة تجريبية فقط. لا تُنشئ أي قيود أو فواتير أو سجلات حقيقية.',close:'إغلاق',ask:'✦ اسأل ONE',aiExplain:'هذه واجهة تمثيلية فقط. لم يتم ربط ذكاء اصطناعي أو بيانات مستخدمين.',aiPlaceholder:'اسأل عن الربح أو المبيعات...',askGo:'عرض إجابة نموذجية',demoAnswer:'الأرقام المعروضة تجريبية وغير مرتبطة بشركة حقيقية.',noRows:'لا توجد نتائج مطابقة',secure:'ليست بيانات حقيقية',actions:'إجراءات تجريبية',context:'الأدوات المرتبطة بهذا القسم',noReal:'لا توجد عملية فعلية أو تكامل متصل',highlight:'مساحة عمل مخصصة لكل عملية',pageNote:'كل اختيار ينتقل إلى عنوان صفحة مستقل. أزرار إنشاء العمليات للعرض فقط.',allApps:'استكشف الموديولات',open:'افتح',performance:'مقارنة الأداء',accountingHealth:'القيود المتوازنة',active:'نشط',new:'جديد',value:'القيمة',sample:'مثال فقط',recordCount:'عدد السجلات',workName:'ONE Business · Demo',homeBtn:'الرئيسية'},
en:{home:'Dashboard',next:'ONE ERP Next',homeHeading:'Business overview',homeDesc:'Every business app in one place. Each operation gets a dedicated workspace.',demo:'Fictional-data preview only. No sign-in or real transactions.',preview:'Interactive preview',fictional:'Fictional data',back:'Back',applications:'Applications',workspace:'Workspace',quick:'Related pages',more:'Full menu',latest:'Recent sample activity',revenue:'Revenue',profit:'Net profit',orders:'Sales orders',invoices:'Invoices',purchases:'Purchase orders',overdue:'Overdue',pulse:'ONE PULSE',activity:'Operations review',selected:'Current page',records:'Illustrative records',search:'Search sample records...',status:'Status',number:'Reference',date:'Date',details:'Description',total:'Amount',currency:'SAR',posted:'Posted',draft:'Draft',pending:'For review',view:'View details',pageTools:'Page tools',period:'October 2026',export:'Export demo CSV',print:'Print',refresh:'Refresh preview',create:'New record',notLive:'Preview action only. It does not create real invoices, journals, or records.',close:'Close',ask:'✦ Ask ONE',aiExplain:'Illustrative experience only. No AI model or user database is connected.',aiPlaceholder:'Ask about profit or sales...',askGo:'Show sample answer',demoAnswer:'All figures are fictional and not connected to a real company.',noRows:'No matching rows',secure:'Not real data',actions:'Sample actions',context:'Tools for this section',noReal:'No live transaction or integration',highlight:'A dedicated workspace for every operation',pageNote:'Each choice loads its own page URL. Action buttons are for demonstration only.',allApps:'Explore modules',open:'Open',performance:'Performance comparison',accountingHealth:'Balanced journals',active:'Active',new:'New',value:'Value',sample:'Sample only',recordCount:'Records',workName:'ONE Business · Demo',homeBtn:'Home'}
};
function t(key){return TR[lang][key]||key}
function tx(ar,en){return lang==='ar'?ar:en}
function norm(x){return String(x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function ico(name,cls){var paths={
home:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
ledger:'<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M8 7h8M8 12h8M8 17h5"/>',
cart:'<circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.5 13h12l2-9H6"/>',
bag:'<path d="M4 7h16l-1 14H5L4 7Z"/><path d="M9 9V6a3 3 0 0 1 6 0v3"/>',
package:'<path d="m12 2 9 5-9 5-9-5 9-5ZM3 7v10l9 5 9-5V7M12 12v10"/>',
users:'<circle cx="9" cy="8" r="3"/><path d="M2 21v-2a7 7 0 0 1 14 0v2M17 5a3 3 0 0 1 0 6M19 14a5 5 0 0 1 3 5v2"/>',
medical:'<path d="M9 2h6v7h7v6h-7v7H9v-7H2V9h7V2Z"/>',
people:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 9h8M8 13h6M8 17h4"/>',
chart:'<path d="M3 3v18h18M7 16l4-5 4 3 5-8"/>',
chevron:'<path d="m6 9 6 6 6-6"/>',
arrow:'<path d="M19 12H5m7 7-7-7 7-7"/>',
right:'<path d="m9 18 6-6-6-6"/>',
search:'<circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/>',
spark:'<path d="m12 2 1.8 7.2L21 11l-7.2 1.8L12 20l-1.8-7.2L3 11l7.2-1.8L12 2Z"/>',
download:'<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
printer:'<path d="M6 9V3h12v6M6 18H4V9h16v9h-2M6 15h12v7H6v-7Z"/>',
calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 2v6M17 2v6M3 11h18"/>',
shield:'<path d="m12 2 9 4v6c0 6-4 9-9 10-5-1-9-4-9-10V6l9-4Z"/><path d="m9 12 2 2 4-4"/>',
plus:'<path d="M12 5v14M5 12h14"/>',
filter:'<path d="M3 4h18l-7 8v7l-4 2v-9L3 4Z"/>',
file:'<path d="M6 2h9l5 5v15H6V2Z"/><path d="M15 2v5h5M9 12h8M9 16h8"/>'
};return '<svg class="'+(cls||'')+'" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(paths[name]||paths.file)+'</svg>'}
function name(m){return tx(m.ar,m.en)}
function label(v){return tx(v[1],v[2])}
function moduleById(id){return MODS.find(function(m){return m.id===id})||MODS[0]}
function viewById(m,id){return m.views.find(function(v){return v[0]===id})||m.views[0]}
function linkTo(m,v){return root+'workspace/?module='+encodeURIComponent(m.id)+'&view='+encodeURIComponent(v[0])}
function homeUrl(){return root}
function num(x){return new Intl.NumberFormat(lang==='ar'?'ar-SA':'en-US',{maximumFractionDigits:0}).format(x)}
function money(x){return '<span class="ltr">'+num(x)+' '+t('currency')+'</span>'}
function getStorage(){try{return localStorage.getItem('one_erp_preview_lang')}catch(e){return null}}
function setStorage(v){try{localStorage.setItem('one_erp_preview_lang',v)}catch(e){}}
function toolbarNav(active){
return '<header class="site-head"><div class="toprow"><div class="topinner"><a class="brand" href="'+homeUrl()+'"><span class="brand-square">ONE</span><span><span class="brand-name">ONE ERP</span><span class="brand-caption">BUSINESS OPERATING SYSTEM</span></span></a><div class="header-tools"><button class="head-ghost hide-small" type="button" data-action="one-ai">'+ico('spark')+' ONE AI</button><button class="head-ghost" type="button" data-action="language" aria-label="Switch language">'+(lang==='ar'?'English':'العربية')+'</button><span class="operator"><span class="bubble">DE</span>'+t('fictional')+'</span></div></div></div>'+
'<div class="navrow"><nav class="navinner" aria-label="'+t('applications')+'"><div class="navrail"><a class="navitem '+(PAGE==='home'?'active':'')+'" href="'+homeUrl()+'">'+ico('home','navicon')+t('home')+'</a>'+MODS.map(function(m){return '<button type="button" class="navitem '+(active&&active.id===m.id?'active':'')+'" data-menu-trigger="'+m.id+'" aria-expanded="false" aria-haspopup="true" aria-label="'+name(m)+'">'+ico(m.icon,'navicon')+norm(name(m))+' '+ico('chevron','chev')+'</button>'}).join('')+'</div><span class="navinfo">'+t('preview')+'</span></nav></div></header><div id="flyout" class="flyout hidden" role="menu" aria-label="'+t('applications')+'"></div>'
}
function demoAlert(){return '<div class="demo-alert"><span>'+ico('shield')+' &nbsp;'+t('demo')+'</span><span class="demo-pill">DEMO DATA</span></div>'}
function foot(){return '<footer class="footer">ONE ERP NEXT · '+t('preview')+' · '+t('secure')+' · © ONE Business</footer>'}
function kpi(lbl,val,icon,note){return '<article class="kpi"><div class="kpi-head"><span class="kpi-label">'+norm(lbl)+'</span><span class="kpi-icon">'+ico(icon)+'</span></div><strong class="kpi-value">'+val+'</strong><div class="kpi-note">'+norm(note||t('sample'))+'</div></article>'}
function chart(){var sales=[298,421,493,635,754,980],profit=[55,91,139,144,191,235];function line(a){return a.map(function(v,i){return (38+i*113)+','+(210-v/1100*170)}).join(' ')}
return '<svg class="linegraph" viewBox="0 0 650 235" role="img" aria-label="Fictional revenue and profit trends" preserveAspectRatio="none"><defs><linearGradient id="chartfill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#cfe3ff"/><stop offset="1" stop-color="#fff" stop-opacity=".2"/></linearGradient></defs><path d="M28 210H632M28 132H632M28 55H632" stroke="#e8eef8" stroke-dasharray="5 6" fill="none"/><polygon points="38,210 '+line(sales)+' 603,210" fill="url(#chartfill)"/><polyline points="'+line(sales)+'" stroke="#2367e5" fill="none" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/><polyline points="'+line(profit)+'" stroke="#16b4a1" fill="none" stroke-width="3" stroke-linecap="round"/><text x="32" y="229" fill="#8497ac" font-size="11">MAY</text><text x="589" y="229" fill="#8497ac" font-size="11">OCT</text></svg>'}
function homeMarkup(){
return toolbarNav(null)+'<main class="wrap">'+demoAlert()+
'<div class="crumbs"><strong>ONE ERP / '+t('home')+'</strong></div><div class="home-head" style="display:flex;gap:12px;align-items:end;justify-content:space-between;flex-wrap:wrap;margin:0 0 24px"><div><span class="eyebrow">ONE BUSINESS · '+t('preview')+'</span><h1>'+t('homeHeading')+'</h1><p>'+t('homeDesc')+'</p></div><a class="btn primary" href="'+linkTo(MODS[0],MODS[0].views[0])+'">'+ico('ledger')+' '+t('applications')+'</a></div>'+
'<div class="kpis">'+kpi(t('orders'),'<span class="ltr">24</span>','cart','↑ 8%')+kpi(t('invoices'),'<span class="ltr">18</span>','file','↑ 12%')+kpi(t('purchases'),'<span class="ltr">11</span>','bag','↑ 5%')+kpi(t('overdue'),'<span class="ltr">4</span>','calendar',t('sample'))+'</div>'+
'<div class="grid"><section class="panel"><div class="panel-title"><h2>✦ '+t('pulse')+'</h2><small>'+t('period')+'</small></div>'+chart()+'<div class="legend"><span><b></b>'+t('revenue')+'</span><span><b class="green"></b>'+t('profit')+'</span></div></section>'+
'<section class="panel"><div class="panel-title"><h2>'+t('latest')+'</h2><small>'+t('sample')+'</small></div><ul class="activity">'+[
[ico('file'),tx('فاتورة مبيعات تجريبية','Sample sales invoice'),tx('لم يتم ترحيل شيء حقيقي','No real posting occurred')],
[ico('package'),tx('استلام مخزون تجريبي','Sample stock receipt'),t('sample')],
[ico('shield'),tx('فحص صلاحيات تجريبي','Sample permission review'),t('sample')],
[ico('calendar'),tx('موعد عيادة تجريبي','Sample clinic appointment'),t('sample')]
].map(function(a){return '<li><span class="activity-bubble">'+a[0]+'</span><div><strong>'+a[1]+'</strong><small>'+a[2]+'</small></div></li>'}).join('')+'</ul></section></div>'+
'<div class="panel-title" style="margin-top:18px"><h2>'+t('allApps')+'</h2><small>'+t('highlight')+'</small></div>'+
'<div class="appgrid">'+MODS.map(function(m){return '<a class="appcard" href="'+linkTo(m,m.views[0])+'"><span class="big">'+ico(m.icon)+'</span><strong>'+norm(name(m))+'</strong><span>'+norm(tx(m.arSub,m.enSub))+'</span><span class="arrow">'+t('open')+' '+ico('right')+'</span></a>'}).join('')+'</div>'+
'<section class="panel" style="margin-top:21px"><div class="panel-title"><h2>'+t('pageNote')+'</h2></div><div class="module-sections">'+MODS.slice(0,3).map(function(m){return '<a class="module-link" href="'+linkTo(m,m.views[1])+'"><span class="bubble">'+ico(m.icon)+'</span><span><strong>'+norm(label(m.views[1]))+'</strong><small>'+norm(tx(m.arSub,m.enSub))+'</small></span></a>'}).join('')+'</div></section>'+
foot()+'</main>'+dialog();
}
function dialog(){return '<div class="overlay hidden" id="dialog" role="dialog" aria-modal="true" aria-labelledby="dialogTitle"><div class="dialog"><h2 id="dialogTitle"></h2><div id="dialogText"></div><div class="dialog-actions"><button type="button" class="btn primary" data-action="close-dialog">'+t('close')+'</button></div></div></div>'}
var currentModule=moduleById(q.get('module')),currentView=viewById(currentModule,q.get('view'));
function genericRowId(prefix,i){return prefix+'-2026-'+String(141-i).padStart(3,'0')}
function demoData(m,v){
var kind=m.id,view=v[0];
var rows,metrics,actions,columns=[t('number'),t('date'),t('details'),t('total'),t('status')],prefix=kind.slice(0,3).toUpperCase();
var chartAccounts=[
 ['1000',tx('النقدية والبنوك','Cash and banks'),580000,0],
 ['1100',tx('ذمم العملاء','Receivables'),430000,0],
 ['1200',tx('المخزون','Inventory'),670000,0],
 ['1500',tx('الأصول الثابتة','Fixed assets'),770000,0],
 ['5000',tx('المصروفات والتكاليف','Expenses and costs'),745000,0],
 ['2000',tx('ذمم الموردين','Accounts payable'),0,520000],
 ['2200',tx('القروض','Loans'),0,600000],
 ['3000',tx('رأس المال','Capital'),0,1095000],
 ['4000',tx('الإيرادات','Revenue'),0,980000]
];
function row(id,when,desc,value,status){return [id,when,desc,value,status]}
var common=[
row(genericRowId(prefix,1),'2026-10-15',tx('عملية تدريبية أ','Sample workflow A'),38400,'posted'),
row(genericRowId(prefix,2),'2026-10-14',tx('عملية تدريبية ب','Sample workflow B'),12600,'pending'),
row(genericRowId(prefix,3),'2026-10-12',tx('عملية تدريبية ج','Sample workflow C'),7500,'draft')
];
rows=common;
metrics=[[t('records'),'24','file'],[t('pending'),'5','calendar'],[t('value'),money(245000),'chart'],[t('active'),'12','shield']];
actions=[[t('create'),'plus'],[t('print'),'printer']];
if(kind==='accounting'){
metrics=[[tx('الأصول','Assets'),money(2450000),'ledger'],[tx('الالتزامات','Liabilities'),money(1120000),'file'],[tx('حقوق الملكية','Equity incl. profit'),money(1330000),'shield'],[t('profit'),money(235000),'chart']];
if(view==='accounts'||view==='statements'){
columns=[t('number'),t('details'),tx('مدين','Debit'),tx('دائن','Credit')];
rows=chartAccounts.map(function(a){return [a[0],a[1],a[2],a[3]]});
if(view==='statements')rows.push([tx('الإجمالي','Total'),'—',3195000,3195000]);
actions=[[tx('تقرير تفصيلي','Detailed report'),'file'],[t('export'),'download'],[t('print'),'printer']];
}else if(view==='overview'){
rows=[row('JE-2026-040','2026-10-15',tx('قيد مبيعات متوازن','Balanced sample journal'),84600,'posted'),row('INV-2026-119','2026-10-14',tx('فاتورة عميل تجريبية','Sample customer invoice'),33700,'pending'),row('PAY-2026-019','2026-10-12',tx('سند صرف تجريبي','Sample payment'),18500,'draft')];
actions=[[tx('فتح القيود اليومية','Open journal entries'),'ledger'],[tx('فتح فواتير العملاء','Open customer invoices'),'file']];
}else if(view==='journals'){
rows=[row('JE-2026-044','2026-10-15',tx('مبيعات تجريبية','Sample sales journal'),84600,'posted'),row('JE-2026-043','2026-10-14',tx('مصروف تجريبي','Sample expense journal'),3500,'posted'),row('JE-2026-042','2026-10-12',tx('تسوية تجريبية','Sample adjustment'),7800,'draft')];
actions=[[tx('قيد جديد','New journal'),'plus'],[tx('قيد تسوية','Adjustment'),'ledger'],[tx('عكس قيد','Reverse journal'),'file']];
}else if(view==='invoices'){
rows=[row('INV-2026-119','2026-10-15',tx('شركة النخبة الافتراضية','Demo Elite Co.'),84600,'posted'),row('INV-2026-118','2026-10-14',tx('شركة المدى الافتراضية','Demo Horizon Co.'),33700,'pending'),row('INV-2026-117','2026-10-12',tx('شركة الأفق الافتراضية','Demo Vista Co.'),9300,'draft')];
metrics=[[t('invoices'),'18','file'],[tx('ذمم العملاء','Receivables'),money(430000),'ledger'],[tx('غير محصلة','Outstanding'),money(93700),'calendar'],[tx('بانتظار المراجعة','Needs review'),'3','shield']];
actions=[[tx('فاتورة جديدة','New invoice'),'plus'],[tx('إشعار دائن','Credit note'),'file'],[tx('كشف حساب عميل','Customer statement'),'ledger']];
}else if(view==='bills'){
rows=[row('BILL-2026-044','2026-10-15',tx('مورد التقنية الافتراضي','Demo Tech Vendor'),42500,'posted'),row('BILL-2026-043','2026-10-13',tx('مورد الأدوات الافتراضي','Demo Tools Vendor'),12500,'pending')];
actions=[[tx('فاتورة مورد جديدة','New vendor bill'),'plus'],[tx('طلب اعتماد','Request approval'),'shield']];
}else if(view==='payments'){
rows=[row('REC-2026-029','2026-10-15',tx('قبض تجريبي','Sample receipt'),32500,'posted'),row('PAY-2026-032','2026-10-13',tx('صرف تجريبي','Sample disbursement'),18500,'pending')];
actions=[[tx('سند قبض','Receive payment'),'plus'],[tx('سند صرف','Make payment'),'file'],[tx('تخصيص دفعة','Allocate payment'),'ledger']];
}else if(view==='bank'){
rows=[row('BNK-2026-010','2026-10-15',tx('حوالة واردة تجريبية','Sample incoming transfer'),32500,'pending'),row('BNK-2026-009','2026-10-13',tx('مطابقة تجريبية','Sample bank match'),18500,'posted')];
actions=[[tx('كشف بنك','Bank statement'),'file'],[tx('تسوية بنكية','Reconcile'),'ledger']];
}else if(view==='tax'){
rows=[row('VAT-Q3-DEMO','2026-09-30',tx('إقرار VAT توضيحي غير قابل للتقديم','Non-filing sample VAT return'),0,'draft'),row('ZAKAT-DEMO','2026-10-15',tx('نموذج الزكاة التجريبي','Sample Zakat model'),0,'draft')];
actions=[[tx('مراجعة ضريبة القيمة المضافة','Review VAT'),'file'],[tx('مراجعة الزكاة','Review Zakat'),'ledger']];
metrics=[[tx('معدل تمثيلي','Illustrative rate'),'15%','chart'],[tx('إقرارات تجريبية','Sample returns'),'4','file'],[tx('اتصال الزكاة والضريبة','ZATCA connection'),tx('غير متصل','Not connected'),'shield'],[tx('قيد المراجعة','Review required'),'2','calendar']];
}
}
if(kind==='sales'){
rows=[row('SO-2026-024','2026-10-15',tx('شركة النخبة الافتراضية','Demo Elite Co.'),84600,'posted'),row('SO-2026-023','2026-10-14',tx('شركة المدى الافتراضية','Demo Horizon Co.'),33700,'pending'),row('Q-2026-022','2026-10-12',tx('عرض سعر تجريبي','Demo quotation'),9300,'draft')];
actions=[[tx('عرض سعر جديد','New quotation'),'plus'],[tx('إنشاء أمر بيع','New sales order'),'cart']];
metrics=[[tx('أوامر البيع','Sales orders'),'24','cart'],[tx('المبيعات','Sales'),money(980000),'chart'],[tx('العروض المفتوحة','Open quotes'),'8','file'],[tx('المتأخرة','Overdue'),'4','calendar']];
}
if(kind==='purchasing'){
rows=[row('PO-2026-011','2026-10-15',tx('مورد التقنية الافتراضي','Demo Tech Vendor'),42500,'posted'),row('PO-2026-010','2026-10-14',tx('مورد الأدوات الافتراضي','Demo Tools Vendor'),12500,'draft'),row('RFQ-2026-009','2026-10-13',tx('عرض شراء تجريبي','Sample RFQ'),7800,'pending')];
actions=[[tx('طلب عرض سعر','New RFQ'),'plus'],[tx('أمر شراء','Purchase order'),'bag']];
metrics=[[tx('الطلبات','Purchase orders'),'11','bag'],[tx('القيمة','Spend'),money(133500),'chart'],[tx('الموردون','Vendors'),'8','users'],[tx('بانتظار الموافقة','For approval'),'2','shield']];
}
if(kind==='inventory'){
rows=[row('ITM-1001','2026-10-15',tx('محرك صناعي تجريبي','Sample industrial motor'),18,'posted'),row('ITM-1002','2026-10-14',tx('صمام هوائي تجريبي','Sample pneumatic valve'),63,'posted'),row('ITM-1003','2026-10-12',tx('حساس ضغط تجريبي','Sample pressure sensor'),27,'pending')];
actions=[[tx('صنف جديد','New product'),'plus'],[tx('حركة مخزون','Stock transfer'),'package']];
metrics=[[tx('الأصناف','Products'),'117','package'],[tx('المستودعات','Warehouses'),'3','home'],[tx('القيمة التقديرية','Estimated value'),money(670000),'chart'],[tx('عمليات معلقة','Pending movements'),'6','calendar']];
}
if(kind==='crm'){
rows=[row('CRM-2026-014','2026-10-15',tx('فرصة تجريبية لشركة النخبة','Sample Elite opportunity'),85000,'pending'),row('CRM-2026-013','2026-10-14',tx('متابعة عميل محتمل','Sample lead follow-up'),44000,'draft')];
actions=[[tx('عميل محتمل','New lead'),'plus'],[tx('تسجيل نشاط','Log activity'),'calendar']];
metrics=[[tx('الفرص','Opportunities'),'12','users'],[tx('قيمة خط المبيعات','Pipeline'),money(312000),'chart'],[tx('المتابعات','Follow-ups'),'5','calendar'],[tx('العملاء المحتملون','Leads'),'18','file']];
}
if(kind==='healthcare'){
rows=[row('MED-2026-003','2026-10-15',tx('سجل مريض تجريبي ٣','Fictional patient 3'),0,'pending'),row('MED-2026-002','2026-10-14',tx('حجز عيادة تجريبي','Sample clinic booking'),0,'posted'),row('MED-2026-001','2026-10-12',tx('طلب تحاليل تجريبي','Sample lab order'),0,'draft')];
actions=[[view==='rooms'?tx('إضافة غرفة','Add room'):view==='appointments'?tx('حجز موعد','Book appointment'):view==='lab'?tx('طلب تحليل','Request test'):view==='pharmacy'?tx('صرف دواء','Dispense medication'):tx('إضافة سجل تجريبي','Add sample record'),'plus'],[tx('عرض الجدول','View schedule'),'calendar']];
metrics=[[tx('الغرف','Rooms'),'42','home'],[tx('المواعيد','Appointments'),'18','calendar'],[tx('فريق الرعاية','Care team'),'12','medical'],[tx('انتظار التحاليل','Lab queue'),'6','file']];
}
if(kind==='hr'){
rows=[row('EMP-2026-003','2026-10-15',tx('موظف تجريبي ٣','Demo employee 3'),0,'posted'),row('EMP-2026-002','2026-10-14',tx('طلب إجازة تجريبي','Sample leave request'),0,'pending'),row('PAY-2026-001','2026-10-12',tx('مسير رواتب تجريبي','Sample payroll run'),18400,'draft')];
actions=[[tx('إضافة موظف','Add employee'),'plus'],[tx('إنشاء مسير','New payroll run'),'file']];
metrics=[[tx('الموظفون','Employees'),'26','users'],[tx('الحضور','Attendance'),'24','calendar'],[tx('المستحقات','Payroll'),money(146000),'ledger'],[tx('طلبات إجازة','Leave requests'),'3','file']];
}
if(kind==='reports'){
rows=[row('RPT-2026-101','2026-10-15',tx('تقرير مالي توضيحي','Sample financial report'),0,'posted'),row('RPT-2026-102','2026-10-14',tx('تقرير حركة المخزون','Sample inventory report'),0,'draft')];
actions=[[t('export'),'download'],[t('print'),'printer']];
metrics=[[tx('التقارير','Reports'),'16','chart'],[tx('المستخدمون','Users'),'8','users'],[tx('الفترات','Periods'),'4','calendar'],[tx('المراجعات','Reviews'),'6','shield']];
}
return{rows:rows,metrics:metrics,actions:actions,columns:columns};
}
function tag(s){return '<span class="tag '+(s==='pending'?'warn':s==='draft'?'bad':'')+'">'+t(s)+'</span>'}
function panelTitle(str,meta){return '<div class="panel-title"><h2>'+norm(str)+'</h2><small>'+norm(meta||t('sample'))+'</small></div>'}
function workspaceMarkup(){
var m=currentModule,v=currentView,data=demoData(m,v);
document.title=label(v)+' | '+name(m)+' | ONE ERP Next Preview';
var breadcrumb='<div class="crumbs"><a href="'+homeUrl()+'">'+t('home')+'</a>'+ico('right')+'<a href="'+linkTo(m,m.views[0])+'">'+norm(name(m))+'</a>'+ico('right')+'<strong>'+norm(label(v))+'</strong></div>';
var actionMarkup=data.actions.map(function(item,i){
var isLink=item[0]===tx('فتح القيود اليومية','Open journal entries')?'journals':item[0]===tx('فتح فواتير العملاء','Open customer invoices')?'invoices':null;
return isLink?'<a class="btn '+(i===0?'primary':'')+'" href="'+linkTo(m,viewById(m,isLink))+'">'+ico(item[1])+' '+norm(item[0])+'</a>':
'<button type="button" data-action="demo" class="btn '+(i===0?'primary':'')+'">'+ico(item[1])+' '+norm(item[0])+'</button>';
}).join('');
var rows=data.rows.map(function(r){return '<tr>'+r.map(function(x,i){return '<td data-label="'+norm(data.columns[i]||'')+'">'+(i===0?'<span class="row-id">'+norm(x)+'</span>':typeof x==='number'?money(x):i===r.length-1&&['posted','pending','draft'].includes(x)?tag(x):norm(x))+'</td>'}).join('')+'</tr>'}).join('');
var contexts=m.views.map(function(item){return '<a class="context-item '+(item[0]===v[0]?'current':'')+'" href="'+linkTo(m,item)+'" '+(item[0]===v[0]?'aria-current="page"':'')+'>'+norm(label(item))+'</a>'}).join('');
return toolbarNav(m)+'<main class="wrap">'+demoAlert()+
'<a href="'+homeUrl()+'" class="workspace-back" id="backBtn">'+ico('arrow')+' '+t('back')+'</a>'+breadcrumb+
'<header class="work-header"><div class="work-identity"><div class="work-emblem">'+ico(m.icon)+'</div><div><div class="eyebrow">ONE ERP NEXT / '+norm(name(m))+'</div><h1>'+norm(label(v))+'</h1><p>'+norm(tx(m.arSub,m.enSub))+' · '+t('highlight')+'</p></div></div><div class="hside"><span class="demo-pill">'+t('preview')+'</span><a class="btn" href="'+linkTo(m,m.views[0])+'">'+ico('home')+' '+t('applications')+'</a></div></header>'+
'<section class="context"><div class="context-heading"><strong>'+t('context')+'</strong><small>'+t('workspace')+' / '+norm(name(m))+'</small></div><nav class="context-strip" aria-label="'+norm(name(m))+'">'+contexts+'</nav></section>'+
'<div class="kpis">'+data.metrics.map(function(k){return kpi(k[0],k[1],k[2],t('sample'))}).join('')+'</div>'+
'<div class="panel">'+panelTitle(label(v),t('period'))+'<div class="work-tools">'+actionMarkup+'<button class="btn" type="button" data-action="export">'+ico('download')+' '+t('export')+'</button><button class="btn" type="button" data-action="print">'+ico('printer')+' '+t('print')+'</button><button class="btn" type="button" data-action="demo" aria-label="'+t('refresh')+'">'+ico('filter')+' '+t('refresh')+'</button></div>'+
'<div class="table-tools"><label style="display:flex;flex:1;gap:10px;align-items:center">'+ico('search')+' <input class="filter" type="search" id="recordSearch" placeholder="'+t('search')+'" aria-label="'+t('search')+'"></label><span class="demo-pill">'+t('fictional')+'</span></div>'+
'<div class="tablewrap"><table><thead><tr>'+data.columns.map(function(c){return '<th>'+norm(c)+'</th>'}).join('')+'</tr></thead><tbody id="dataRows">'+rows+'</tbody></table></div>'+
'<div class="table-foot"><span>'+t('recordCount')+': '+data.rows.length+'</span><span>🔒 '+t('noReal')+'</span></div></div>'+
'<section class="panel">'+panelTitle(t('quick'),t('more'))+'<div class="module-sections">'+m.views.filter(function(x){return x[0]!==v[0]}).slice(0,6).map(function(x){return '<a class="module-link" href="'+linkTo(m,x)+'"><span class="bubble">'+ico(m.icon)+'</span><span><strong>'+norm(label(x))+'</strong><small>'+norm(tx(m.arSub,m.enSub))+'</small></span></a>'}).join('')+'</div></section>'+
'<div class="notice">'+ico('shield')+' '+t('pageNote')+' '+t('noReal')+'</div>'+foot()+'</main>'+dialog();
}
var opened=null,lastTrigger=null;
function hideMenu(){var el=document.getElementById('flyout');if(el)el.classList.add('hidden');document.querySelectorAll('[data-menu-trigger]').forEach(function(b){b.setAttribute('aria-expanded','false')});opened=null}
function showMenu(id,b){var m=moduleById(id),menu=document.getElementById('flyout');if(!menu||!b)return;
if(opened===id){hideMenu();return}
opened=id;lastTrigger=b;
document.querySelectorAll('[data-menu-trigger]').forEach(function(btn){btn.setAttribute('aria-expanded',String(btn===b))});
menu.innerHTML='<div class="flyout-head"><span class="circle">'+ico(m.icon)+'</span><span><b>'+norm(name(m))+'</b><small>'+norm(tx(m.arSub,m.enSub))+'</small></span></div>'+
'<div class="flygroup">'+t('actions')+'</div>'+
m.views.map(function(v){return '<a role="menuitem" class="flylink" '+(PAGE==='workspace'&&currentModule.id===m.id&&currentView[0]===v[0]?'aria-current="page"':'')+' href="'+linkTo(m,v)+'">'+ico(v[0]==='overview'?'home':m.icon)+' <span>'+norm(label(v))+'</span><span class="go">'+ico('right')+'</span></a>'}).join('')+
'<div class="flyout-foot">'+ico('shield')+' '+t('noReal')+'</div>';
menu.classList.remove('hidden');positionMenu(b,menu);
}
function positionMenu(b,menu){var rect=b.getBoundingClientRect(),width=menu.offsetWidth||310;var rtl=lang==='ar';var left=rtl?Math.max(12,Math.min(window.innerWidth-width-12,rect.right-width)):Math.max(12,Math.min(window.innerWidth-width-12,rect.left));menu.style.left=left+'px';menu.style.top=Math.min(window.innerHeight-150,rect.bottom+5)+'px'}
function downloadSampleCSV(){
if(PAGE!=='workspace')return;
var d=demoData(currentModule,currentView);
function safe(x){
 var str=String(x).replace(/<[^>]+>/g,'').trim();
 if(/^[=+\-@\t\r]/.test(str))str="'"+str;
 return '"'+str.replace(/"/g,'""')+'"';
}
var rows=[d.columns].concat(d.rows);
var content='\uFEFF'+rows.map(function(r){return r.map(safe).join(',')}).join('\r\n');
var blob=new Blob([content],{type:'text/csv;charset=utf-8'});
var obj=URL.createObjectURL(blob);
var a=document.createElement('a');
a.href=obj;a.download='ONE_ERP_NEXT_DEMO_'+currentModule.id+'_'+currentView[0]+'.csv';
document.body.appendChild(a);a.click();a.remove();
setTimeout(function(){URL.revokeObjectURL(obj)},1000);
}
function modal(){document.getElementById('dialog').classList.remove('hidden');document.getElementById('dialogTitle').textContent=t('preview');document.getElementById('dialogText').innerHTML='<p>'+t('notLive')+'</p><p>'+t('demoAnswer')+'</p>'}
function closeDialog(){document.getElementById('dialog').classList.add('hidden')}
function showAI(){document.getElementById('dialog').classList.remove('hidden');document.getElementById('dialogTitle').textContent=t('ask');document.getElementById('dialogText').innerHTML='<p>'+t('aiExplain')+'</p><input id="askText" maxlength="110" class="filter" style="max-width:none;width:100%" placeholder="'+norm(t('aiPlaceholder'))+'"><button class="btn primary" type="button" data-action="answer" style="margin-top:12px">'+t('askGo')+'</button><div id="aiResult"></div>'}
function filterTable(value){var rows=document.querySelectorAll('#dataRows tr'),count=0;rows.forEach(function(r){var visible=r.textContent.toLocaleLowerCase().includes(value.toLocaleLowerCase());r.hidden=!visible;if(visible)count++});var tbody=document.getElementById('dataRows'),oldEmpty=document.getElementById('noMatchRow');
if(oldEmpty)oldEmpty.remove();
if(!count&&rows.length){var tr=document.createElement('tr');tr.id='noMatchRow';var td=document.createElement('td');td.colSpan=rows[0].cells.length;td.textContent=t('noRows');td.style.textAlign='center';td.style.padding='25px';tr.appendChild(td);tbody.appendChild(tr)}
}
function setupEvents(){
document.addEventListener('click',function(e){var trigger=e.target.closest('[data-menu-trigger]');if(trigger){showMenu(trigger.dataset.menuTrigger,trigger);return}
if(opened&&!e.target.closest('#flyout'))hideMenu();
var b=e.target.closest('[data-action]');if(!b)return;
if(b.dataset.action==='language'){setStorage(lang==='ar'?'en':'ar');var scroll=window.scrollY;location.reload();return}
if(b.dataset.action==='one-ai'){showAI();return}
if(b.dataset.action==='demo'){modal();return}
if(b.dataset.action==='print'){window.print();return}
if(b.dataset.action==='export'){downloadSampleCSV();return}
if(b.dataset.action==='close-dialog'){closeDialog();return}
if(b.dataset.action==='answer'){var q=document.getElementById('askText').value||'';var answer=/ربح|profit/i.test(q)?t('profit')+': '+money(235000):/مبيع|sales/i.test(q)?t('revenue')+': '+money(980000):/فاتور|invoice/i.test(q)?t('invoices')+': 18':t('demoAnswer');document.getElementById('aiResult').innerHTML='<div class="notice">'+answer+'<br>'+t('demoAnswer')+'</div>';return}
});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){hideMenu();closeDialog()}});
document.addEventListener('input',function(e){if(e.target.id==='recordSearch')filterTable(e.target.value)});
document.addEventListener('click',function(e){if(e.target.id==='dialog')closeDialog()});
window.addEventListener('resize',function(){if(opened&&lastTrigger){var menu=document.getElementById('flyout');if(menu)positionMenu(lastTrigger,menu)}});
window.addEventListener('scroll',function(e){if(!opened)return;var target=e.target;if(target&&typeof target.closest==='function'&&target.closest('#flyout'))return;hideMenu()},true);
var back=document.getElementById('backBtn');
if(back)back.addEventListener('click',function(e){
 try{var ref=new URL(document.referrer);if(ref.origin===location.origin&&ref.pathname.indexOf('/one-erp-preview/')!==-1){e.preventDefault();history.back()}}catch(err){}
});
}
function main(){
document.documentElement.lang=lang;
document.documentElement.dir=lang==='ar'?'rtl':'ltr';
document.title=(PAGE==='home'?t('homeHeading'):label(currentView))+' | ONE ERP Next Preview';
document.getElementById('app').innerHTML=PAGE==='home'?homeMarkup():workspaceMarkup();
setupEvents();
}
main();
})();
