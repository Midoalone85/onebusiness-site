const megaBtn=document.querySelector('.mega-btn');const mega=document.querySelector('.mega');
function closeMega(){if(mega&&megaBtn){mega.classList.remove('show');megaBtn.classList.remove('active');megaBtn.setAttribute('aria-expanded','false')}}
if(megaBtn&&mega){megaBtn.addEventListener('click',e=>{e.stopPropagation();const open=!mega.classList.contains('show');mega.classList.toggle('show',open);megaBtn.classList.toggle('active',open);megaBtn.setAttribute('aria-expanded',String(open))});mega.addEventListener('click',e=>e.stopPropagation());document.addEventListener('click',closeMega)}
const mt=document.querySelector('.mobile-toggle'),mm=document.querySelector('.mobile-menu'),mc=document.querySelector('.mobile-close');function setMobileMenu(open){if(!mt||!mm)return;mm.classList.toggle('show',open);mt.setAttribute('aria-expanded',String(open));document.body.classList.toggle('menu-open',open);mt.setAttribute('aria-label',open?'إغلاق القائمة':'فتح القائمة')}if(mt&&mm){mt.addEventListener('click',()=>setMobileMenu(!mm.classList.contains('show')));mc&&mc.addEventListener('click',()=>setMobileMenu(false));mm.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>setMobileMenu(false)))}
const answers=[null,null,null,null];document.querySelectorAll('.question').forEach(q=>{q.querySelectorAll('button').forEach(btn=>btn.addEventListener('click',()=>{const qi=Number(q.dataset.q),v=Number(btn.dataset.v);answers[qi]=v;q.querySelectorAll('button').forEach(b=>b.classList.remove('active'));btn.classList.add('active');const done=answers.filter(v=>v!==null).length;const scoreEl=document.getElementById('score'),label=document.getElementById('score-label');if(scoreEl){const score=Math.round((answers.reduce((a,b)=>a+(b??0),0)/8)*100);scoreEl.textContent=done===4?score+'%':Math.round((done/4)*100)+'%';if(label)label.textContent=done===4?(score<40?'ابدأ بتثبيت العمليات والبيانات':score<75?'أنت جاهز للربط والأتمتة المرحلية':'جاهزية قوية، ركز على التحسين والذكاء'):done+' من 4 أسئلة تم الإجابة عنها'}}))});
const form=document.getElementById('contact-form');if(form){form.addEventListener('submit',e=>{e.preventDefault();form.querySelector('.fields').hidden=true;form.querySelector('.form-success').hidden=false});const reset=form.querySelector('.reset-form');reset&&reset.addEventListener('click',()=>{form.reset();form.querySelector('.fields').hidden=false;form.querySelector('.form-success').hidden=true})}
window.addEventListener('pageshow',()=>{if(location.hash.startsWith('#')&&location.pathname.includes('/services/'))window.scrollTo(0,0)});

document.querySelectorAll('.mega-card').forEach(a=>a.addEventListener('click',closeMega));

// Enterprise navigation polish
const headerWrap=document.querySelector('.header-wrap');
const syncHeader=()=>headerWrap&&headerWrap.classList.toggle('scrolled',window.scrollY>24);
syncHeader();window.addEventListener('scroll',syncHeader,{passive:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeMega();setMobileMenu(false);megaBtn&&megaBtn.focus()}});

// Project brief helper and service preselection
const serviceLabels={
  'business-development':'تطوير الأعمال',
  'web-commerce':'المواقع والمتاجر',
  'mobile-apps':'التطبيقات',
  'erp-finance':'ERP والأنظمة المالية',
  'ai-automation':'الأتمتة والذكاء الاصطناعي',
  'sales-growth':'المبيعات والنمو',
  'unsure':'مش متأكد، محتاج تشخيص'
};
const params=new URLSearchParams(location.search);const requestedService=params.get('service');
if(requestedService){
  const projectSelect=document.getElementById('project-service');if(projectSelect&&serviceLabels[requestedService])projectSelect.value=requestedService;
  const homeSelect=document.querySelector('#contact-form select[name="service"]');if(homeSelect&&serviceLabels[requestedService]){
    [...homeSelect.options].forEach(o=>{if(o.textContent.trim()===serviceLabels[requestedService])homeSelect.value=o.value});
  }
}
const projectForm=document.getElementById('project-form');
if(projectForm){
  projectForm.addEventListener('submit',e=>{
    e.preventDefault();const data=new FormData(projectForm);const label=serviceLabels[data.get('service')]||data.get('service')||'غير محدد';
    const summary=`ONE Business — ملخص مشروع\n\nالاسم: ${data.get('name')||''}\nالشركة: ${data.get('company')||''}\nالجوال: ${data.get('phone')||''}\nالبريد: ${data.get('email')||''}\nالخدمة: ${label}\nمرحلة الشركة: ${data.get('stage')||''}\nالأولوية: ${data.get('priority')||''}\n\nالوضع الحالي والنتيجة المطلوبة:\n${data.get('details')||''}`;
    const success=projectForm.querySelector('.project-success');const textarea=projectForm.querySelector('.brief-summary');
    [...projectForm.children].forEach(el=>{if(el!==success)el.hidden=true});textarea.value=summary;success.hidden=false;success.scrollIntoView({behavior:'smooth',block:'center'});
    const whatsappUrl=`https://wa.me/966564581924?text=${encodeURIComponent(summary)}`;
    window.open(whatsappUrl,'_blank','noopener,noreferrer');
  });
  const copy=projectForm.querySelector('.copy-brief');copy&&copy.addEventListener('click',async()=>{const value=projectForm.querySelector('.brief-summary').value;try{await navigator.clipboard.writeText(value);copy.textContent='تم النسخ ✓';setTimeout(()=>copy.textContent='نسخ الملخص',1800)}catch{projectForm.querySelector('.brief-summary').select();document.execCommand('copy')}});
  const resetProject=projectForm.querySelector('.reset-project');resetProject&&resetProject.addEventListener('click',()=>{const success=projectForm.querySelector('.project-success');success.hidden=true;[...projectForm.children].forEach(el=>{if(el!==success)el.hidden=false});projectForm.scrollIntoView({behavior:'smooth',block:'start'})});
}


// Final motion and route UX
const revealTargets=document.querySelectorAll('.service-card,.sector,.step,.deliverable,.work-case,.belief-grid article,.manifest-values>div,.showcase-grid>article,.packages>article,.related-card');
if('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches){
  revealTargets.forEach(el=>el.classList.add('reveal'));
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('in-view');observer.unobserve(entry.target)}}),{threshold:.08,rootMargin:'0px 0px -35px'});
  revealTargets.forEach(el=>observer.observe(el));
}else{revealTargets.forEach(el=>el.classList.add('in-view'))}

// Keep true page navigation predictable: service links always load at the top of their page.
document.querySelectorAll('a[href]').forEach(a=>{const href=a.getAttribute('href')||'';if(/services\/[a-z-]+\/?(?:\?.*)?$/.test(href)){a.addEventListener('click',()=>sessionStorage.setItem('one:navigate-top','1'))}});
if(sessionStorage.getItem('one:navigate-top')==='1'){sessionStorage.removeItem('one:navigate-top');window.scrollTo(0,0)}
