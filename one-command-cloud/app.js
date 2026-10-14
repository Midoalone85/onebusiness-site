const $=id=>document.getElementById(id);let current=null,lastSessionDuration=null,currentPhoneId=null,phonePolling=false,pendingAssist=null,lastMonitorInterval=null;
function durationHint(d){
 if(!d.active)return 'الجلسة متوقفة. اختار المدة ثم اضغط ابدأ الفحص.';
 if(d.unlimited)return 'بدون حد زمني • مستمرة حتى إيقافها أو إعادة تشغيل الاستضافة • '+(d.pendingJobs||0)+' مهام في الانتظار';
 const date=new Date(d.expires);
 return 'تنتهي: '+date.toLocaleString('ar-SA')+' • '+(d.pendingJobs||0)+' مهام في الانتظار';
}
const BACKUP_KEY='onecommand:planning:local:v1',BACKUP_DISABLED_KEY='onecommand:planning:disabled:v1';
function readBrowserBackup(){
 try{const raw=localStorage.getItem(BACKUP_KEY);if(!raw||raw.length>80000)return null;
  const data=JSON.parse(raw);return data&&Array.isArray(data.changes)&&data.changes.length<=40?data:null;
 }catch{return null}
}
function updateBrowserBackup(changes){
 try{
  const disabled=localStorage.getItem(BACKUP_DISABLED_KEY)==='1';
  $('forgetBrowser').textContent=disabled?'تفعيل الحفظ في المتصفح':'مسح نسخة المتصفح';
  if(!disabled&&Array.isArray(changes)&&changes.length){
   const data={type:'planning_backup',version:1,product:'ONE COMMAND',savedAt:new Date().toISOString(),changes};
   const previous=readBrowserBackup(),same=previous&&JSON.stringify(previous.changes)===JSON.stringify(changes);
   if(!same)localStorage.setItem(BACKUP_KEY,JSON.stringify(data));
  }
  const cached=readBrowserBackup();
  val('browserBackupHint',disabled?'الحفظ المحلي متوقف حتى تعيد تفعيله':cached?'نسخة هذا المتصفح تحتوي '+cached.changes.length+' طلب. متاحة للاستعادة بعد إعادة تشغيل الاستضافة.':'لا توجد نسخة محلية للطلبات بعد.');
 }catch{val('browserBackupHint','تعذر حفظ نسخة المتصفح. استخدم تنزيل ملف JSON.')}
}
function showOracleServices(results){
 const host=($('oracleServices'));if(!host)return;
 const data=(results||[]).find(r=>r.kind==='oracle_health')?.data;
 if(!data){host.textContent='لم يصل فحص جديد لـOracle بعد.';return}
 if(data.connected!==true){host.textContent='تعذر فحص Oracle عبر SSH. راجع اتصال Windows والمفتاح المعروف.';return}
 if(data.dockerInspectionAvailable===false){host.textContent='تم الاتصال بـOracle لكن تعذر قراءة حالة Docker. افحص صلاحيات Docker والاتصال، دون إعادة تشغيل أي خدمة.';return}
 const services=data.services||{};
 const entries=[['oneerp-v3','ONE ERP'],['oneerp-db','PostgreSQL'],['oneerp-v3-https','HTTPS'],['oneerp-v3-public','HTTP'],['oneerp-v3-acme','SSL Certificates'],['oneerp-v3-gzip','Compression']];
 if(!Object.keys(services).length){host.textContent='النتيجة من وكيل قديم. أعد فحص Oracle للحصول على خدمات Docker.';return}
 host.textContent=entries.map(([id,name])=>(services[id]?.running?'✓ ':'⚠ ')+name+': '+(services[id]?.running?'يعمل':'غير ظاهر ضمن الحاويات الجارية')).join('\n')+
 '\nآخر فحص: '+new Date(data.checkedAt).toLocaleString('ar-SA');
}
function showJobTimeline(items){
 const status={queued:'⏳ في الانتظار',running:'◉ جارٍ الفحص',completed:'✓ اكتمل',expired:'⚠ انتهت المهلة',cancelled:'◌ أُلغي'};
 val('jobTimeline',(items||[]).slice(0,10).map(j=>(status[j.status]||j.status)+' • '+j.kind+' • '+new Date(j.updated).toLocaleTimeString('ar-SA')).join('\n')||'لم تبدأ أي فحوص بعد.');
}
async function request(url,method='GET',obj){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:obj?JSON.stringify(obj):undefined,credentials:'same-origin'});let v=await r.json();if(r.status===401&&url!=='/api/login'){$('app').hidden=true;$('login').hidden=false;return null}if(!r.ok)throw Error(v.error||'تعذر الاتصال');return v}
function val(id,text){$(id).textContent=text}
function renderChanges(list){
 const out=$('changeList');out.replaceChildren();
 if(!list.length){out.textContent='لا توجد طلبات حاليًا.';return}
 for(const item of list){
  const el=document.createElement('div');el.className='change-row';
  const head=document.createElement('strong');head.textContent=item.title+' • '+item.target;el.appendChild(head);
  const status=document.createElement('span');status.className='pill';status.textContent=({pending_review:'بانتظار المراجعة',reviewed:'تمت المراجعة فقط',rejected:'مرفوض'})[item.status]||item.status;el.appendChild(status);
  const detail=document.createElement('p');detail.textContent=item.details;el.appendChild(detail);
  const act=document.createElement('div');act.className='actions';el.appendChild(act);
  const share=document.createElement('button');share.type='button';share.className='soft';share.textContent='نسخ المهمة لـChatGPT';
  share.onclick=async()=>{const prompt='راجع طلب تطوير ONE COMMAND التالي، حلّل المخاطر واقترح خطة واختبارات. لا تنفذ أي تعديل إنتاجي بدون موافقتي. المشروع: '+item.target+'، المهمة: '+item.title+'، التفاصيل: '+item.details;try{await navigator.clipboard.writeText(prompt);alert('تم نسخ الطلب. الصقه في ChatGPT للمتابعة.')}catch{alert('تعذر نسخ النص')}};act.appendChild(share);
  if(item.status==='reviewed'&&['Oracle','ONE ERP','Windows'].includes(item.target)){
   const probe=document.createElement('button');probe.type='button';probe.className='soft';
   probe.textContent='▶ طلب فحص آمن فقط';probe.disabled=!(current&&current.active&&current.agentOnline);
   probe.title='يتطلب جلسة نشطة ووكيل Windows متصل';
   probe.onclick=async()=>{if(!confirm('سيتم طلب فحوص قراءة فقط، دون تعديل السيرفر أو ERP. هل توافق؟'))return;
    try{const d=await request('/api/changes/check','POST',{id:item.id});await refresh();alert('تم إرسال طلب فحص '+(d.kinds||[]).join(', '))}
    catch(error){alert(error.message)}
   };act.appendChild(probe);
 }
 if(item.status==='pending_review'){
   for(const [state,label] of [['reviewed','تمت المراجعة'],['rejected','رفض الطلب']]){
    const btn=document.createElement('button');btn.type='button';btn.className='soft';btn.textContent=label;
    btn.onclick=async()=>{try{await request('/api/changes/review','POST',{id:item.id,status:state});await refresh()}catch(e){alert(e.message)}};act.appendChild(btn);
   }
  }out.appendChild(el);
 }
}
function renderPhones(phones,d){
 const sel=$('phoneDevices'),prev=sel.value;sel.replaceChildren();
 const none=document.createElement('option');none.value='';none.textContent='اختر الهاتف المتصل';sel.appendChild(none);
 for(const p of phones){const option=document.createElement('option');option.value=p.id;option.textContent=p.label+' • '+p.platform+(p.lastAction?' • آخر رد '+new Date(p.lastAction.at).toLocaleTimeString('ar-SA'):'');sel.appendChild(option)}
 if(phones.some(p=>p.id===prev))sel.value=prev;
 else if(phones.length===1)sel.value=phones[0].id;
 const enable=!!(d.active&&d.controlTarget==='mobile'&&sel.value);
 $('pingPhone').disabled=!enable;$('buzzPhone').disabled=!enable;
 const linked=phones.find(p=>p.id===currentPhoneId);
 $('disconnectPhone').disabled=!linked;
 val('phoneStatus',phones.length?'هواتف متصلة: '+phones.length+'. الأوامر تعمل داخل المتصفح فقط وتحتاج الصفحة مفتوحة.':'لا يوجد متصفح هاتف متصل الآن. اربط الهاتف أولًا.');
}
async function registerThisPhone(){
 if(currentPhoneId)return true;
 const id=crypto.randomUUID(),ua=navigator.userAgent||'';
 const platform=/iPhone|iPad/i.test(ua)?'iOS':/Android/i.test(ua)?'Android':'Desktop browser';
 const label=platform==='Desktop browser'?'متصفح هذا الجهاز':'هاتفي '+platform;
 const x=await request('/api/mobile/register','POST',{id,platform,label});
 if(!x)return false;currentPhoneId=id;
 val('phoneLocalStatus','متصفح هذا الجهاز مرتبط الآن. يمكن إغلاق الربط من زر «فصل هذا الهاتف».');
 await phoneHeartbeat();await refresh();return true;
}
async function phoneHeartbeat(){
 if(!currentPhoneId||phonePolling)return;
 phonePolling=true;
 try{
  const out=await request('/api/mobile/heartbeat','POST',{id:currentPhoneId});
  if(!out)return;
  if(out.command){
   let supported=true;
   if(out.command.kind==='vibrate'){supported=typeof navigator.vibrate==='function'?navigator.vibrate([220,100,220]):false}
   val('phoneLocalStatus',out.command.kind==='ping'?'تم استلام اختبار اتصال من لوحة ONE COMMAND.':'تم استلام أمر الاهتزاز. '+(supported?'تم إرسال طلب الاهتزاز للمتصفح.':'المتصفح لا يدعم الاهتزاز أو رفضه.'));
   await request('/api/mobile/ack','POST',{id:currentPhoneId,commandId:out.command.id,supported});
  }
 }catch(e){val('phoneLocalStatus','توقف اتصال الهاتف: '+e.message+'. أعد الربط من هذا الجهاز.');currentPhoneId=null}
 finally{phonePolling=false}
}
$('pairPhone').onclick=async()=>{try{await registerThisPhone()}catch(e){val('phoneLocalStatus','تعذر الربط: '+e.message)}};
$('disconnectPhone').onclick=async()=>{if(!currentPhoneId)return;try{await request('/api/mobile/disconnect','POST',{id:currentPhoneId});currentPhoneId=null;val('phoneLocalStatus','تم فصل متصفح الهاتف.');await refresh()}catch(e){alert(e.message)}};
async function phoneAction(kind){const id=$('phoneDevices').value;if(!id)return alert('اختر الهاتف أولًا');
 try{const r=await request('/api/mobile/action','POST',{id,kind});if(r){val('phoneStatus','تم إرسال '+(kind==='ping'?'اختبار اتصال':'تنبيه اهتزاز')+'، وجار انتظار رد المتصفح...');await refresh()}}catch(e){alert(e.message)}}
async function allowedWindowsAction(app){
 const commands={notepad:'افتح المفكرة',calculator:'افتح الآلة الحاسبة',erp:'افتح one erp',console:'افتح one command'};
 const text=commands[app];if(!text)return;
 try{
  const preview=await request('/api/assist/preview','POST',{text});
  if(!preview)return;
  if(!confirm('طلب '+preview.label+' على الكمبيوتر المتصل. الموافقة لمرة واحدة فقط. هل توافق؟'))return;
  const result=await request('/api/assist/execute','POST',{text,approved:true,requestId:preview.requestId});
  if(result){val('windowsActionStatus','تم إرسال '+result.label+'. راجع نتيجة الوكيل في سجل العمليات.');await refresh()}
 }catch(e){val('windowsActionStatus','تعذر إرسال الطلب: '+e.message)}
}
$('openNotepad').onclick=()=>allowedWindowsAction('notepad');
$('openCalculator').onclick=()=>allowedWindowsAction('calculator');
$('openERP').onclick=()=>allowedWindowsAction('erp');
$('openConsole').onclick=()=>allowedWindowsAction('console');
$('pingPhone').onclick=()=>phoneAction('ping');
$('buzzPhone').onclick=()=>phoneAction('vibrate');
function resetAssistPreview(){pendingAssist=null;$('assistPreview').hidden=true}
$('assistForm').onsubmit=async e=>{
 e.preventDefault();resetAssistPreview();
 const value=$('assistText').value.trim();
 try{
  const plan=await request('/api/assist/preview','POST',{text:value});
  if(!plan)return;
  pendingAssist={text:value,label:plan.label,requestId:plan.requestId,expiresAt:plan.expiresAt};
  val('assistLabel','المقترح: '+plan.label+' (قائمة سماح محددة)');
  $('assistPreview').hidden=false;
  val('assistStatus',current?.active&&current?.controlTarget==='computer'?'الموافقة صالحة لدقيقتين ولمرة واحدة.':'ابدأ جلسة الكمبيوتر ثم اعرض الأمر من جديد للموافقة.');
 }catch(err){val('assistStatus',err.message)}
};
$('cancelAssist').onclick=()=>{resetAssistPreview();val('assistStatus','تم إلغاء الأمر المقترح.')};
$('approveAssist').onclick=async()=>{
 if(!pendingAssist)return;
 const proposed=pendingAssist;
 if(!confirm('توافق على: '+proposed.label+'؟ لن تنفذ أي أوامر خارج القائمة المسموحة.'))return;
 try{const r=await request('/api/assist/execute','POST',{text:proposed.text,approved:true,requestId:proposed.requestId});
   if(r){resetAssistPreview();val('assistStatus','تم إرسال: '+r.label+' ('+r.queued+' مهمة). تابع حالة الفحوص.');await refresh()}
 }catch(err){val('assistStatus','لم يتم التنفيذ: '+err.message)}
};
$('applyMonitor').onclick=async()=>{
 const interval=$('monitorInterval').value;
 if(interval!=='off'&&!confirm('توافق على تشغيل فحوص تلقائية قراءة فقط أثناء جلسة الكمبيوتر؟'))return;
 try{const r=await request('/api/control/monitor','POST',{interval});if(r){lastMonitorInterval=interval;await refresh()}}
 catch(e){alert(e.message)}
};
async function refresh(){try{const d=await request('/api/state');if(!d)return;current=d;$('login').hidden=true;$('app').hidden=false;$('applyMonitor').disabled=!(d.active&&d.controlTarget==='computer');
const mi=d.monitor?.interval||'off';
if(mi!==lastMonitorInterval){$('monitorInterval').value=mi;lastMonitorInterval=mi}
val('monitorStatus',mi==='off'?'المراقبة الدورية متوقفة.':('المراقبة مفعّلة. الفحص التالي: '+(d.monitor?.nextAt?new Date(d.monitor.nextAt).toLocaleString('ar-SA'):'غير محدد')+'. يتم إيقافها عند انتهاء الجلسة أو إعادة التشغيل.'));
val('agentState',d.agentOnline?'متصل الآن ●':'غير متصل ○');$('agentState').className='metric '+(d.agentOnline?'good':'bad');val('agentHint',d.agentOnline?'الوكيل يستجيب ويستطيع استقبال طلبات الفحص':'يجب تشغيل Windows Agent على جهازك أولًا');$('agentBar').style.width=d.agentOnline?'100%':'9%';val('sessionState',d.active?'نشطة ✓':'متوقفة');$('controlTarget').disabled=d.active;if(d.active)$('controlTarget').value=d.controlTarget||'computer';$('start').disabled=d.active;$('stop').disabled=!d.active;$('scan').disabled=!(d.active&&d.controlTarget!=='mobile');$('openNotepad').disabled=!(d.active&&d.controlTarget==='computer'&&d.agentOnline);$('openCalculator').disabled=$('openNotepad').disabled;$('openERP').disabled=$('openNotepad').disabled;$('openConsole').disabled=$('openNotepad').disabled;$('oracleCheck').disabled=!(d.active&&d.controlTarget!=='mobile');val('count',String(d.results.length));val('sessionHint',durationHint(d));$('applyDuration').disabled=!d.active;if(d.active&&lastSessionDuration!==d.sessionDuration){$('sessionDuration').value=d.sessionDuration;lastSessionDuration=d.sessionDuration}if(!d.active)lastSessionDuration=null;renderChanges(d.changes||[]);updateBrowserBackup(d.changes||[]);showJobTimeline(d.jobs||[]);showOracleServices(d.results||[]);renderPhones(d.phones||[],d);const g=d.diagnosis||{};val('diagnosis',[g.title||'في انتظار نتائج الفحص',...(g.problems||[]).map(x=>'⚠ '+x),...(g.ok||[]).map(x=>'✓ '+x),...(g.suggestions||[]).map(x=>'↳ '+x),g.disclaimer||''].filter(Boolean).join('\n'));$('analyze').disabled=!(d.active&&d.aiConfigured&&d.results.length);val('aiStatus',d.aiConfigured?'AI API متصل بالإعدادات. اضغط للتحليل بعد موافقتك على مشاركة التقرير.':'التحليل الآلي يحتاج إعداد OpenAI API key. الفحص بدون AI متاح دائمًا.');val('results',d.results.length?d.results.map(x=>new Date(x.at).toLocaleString('ar-SA')+' | '+x.kind+'\n'+JSON.stringify(x.data,null,2)).join('\n\n'):'لا توجد تقارير بعد.');val('events',d.events.map(x=>new Date(x.at).toLocaleTimeString('ar-SA')+' • '+x.message).join('\n')||'لا توجد عمليات بعد.')}catch(e){console.warn(e.message);val('agentHint','تعذر تحديث الحالة. تحقق من الاتصال وأعد المحاولة.')}}
$('loginForm').onsubmit=async e=>{e.preventDefault();try{await request('/api/login','POST',{password:$('password').value.trim()});$('password').value='';val('loginError','');await refresh()}catch(e){val('loginError',e.message)}};
async function action(path){try{await request(path,'POST',{});await refresh()}catch(e){alert(e.message)}}
async function setDuration(start){
 const duration=$('sessionDuration').value,target=$('controlTarget').value;
 if(start&&target==='mobile'&&!currentPhoneId&&/Android|iPhone|iPad|Mobile/i.test(navigator.userAgent))await registerThisPhone();
 if(duration==='unlimited'&&!confirm('جلسة الفحص بدون حد زمني ستظل نشطة حتى توقفها يدويًا أو تُعاد الاستضافة للتشغيل. تسجيل الدخول نفسه ينتهي بعد 30 دقيقة، والفحوص قراءة فقط. هل توافق؟'))return;
 try{await request(start?'/api/control/start':'/api/control/duration','POST',start?{duration,target}:{duration});await refresh()}
 catch(e){alert(e.message)}
}
$('start').onclick=()=>setDuration(true);$('applyDuration').onclick=()=>setDuration(false);$('stop').onclick=()=>action('/api/control/stop');$('scan').onclick=()=>action('/api/control/scan');$('refresh').onclick=refresh;$('oracleCheck').onclick=()=>action('/api/control/oracle');
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('results').textContent);alert('تم نسخ النتائج')}catch{alert('تعذر نسخ النتائج')}};
$('export').onclick=()=>{if(!current)return alert('لا توجد بيانات محفوظة في هذه الجلسة');const data={product:'ONE COMMAND',exportedAt:new Date().toISOString(),diagnosis:current.diagnosis,results:current.results,events:current.events,changes:current.changes};const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='ONE_COMMAND_Report_'+new Date().toISOString().slice(0,10)+'.json';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)};
$('chat').onclick=async()=>{const msg='استخدم Desktop Commander المتصل لفحص جهاز Windows وONE ERP قراءة فقط. طلب ONE COMMAND الحالي: '+$('results').textContent.slice(0,1700)+' . لا تعدل بيانات ولا تعيد تشغيل الخدمات قبل موافقتي.';try{await navigator.clipboard.writeText(msg);alert('تم نسخ طلب التحكم. ألصقه في ChatGPT وأرسله للبدء.')}catch{}window.open('https://chatgpt.com/','_blank','noopener')};
$('analyze').onclick=async()=>{if(!confirm('هل توافق على إرسال بيانات أحدث فحوصك إلى خدمة OpenAI API لتحليلها؟ لن يتم تنفيذ أوامر على الجهاز.'))return;try{$('analyze').disabled=true;val('aiAnswer','جار التحليل...');const d=await request('/api/ai/analyze','POST',{consent:true});if(d)val('aiAnswer',d.answer||'لم يصل تحليل');}catch(e){val('aiAnswer',e.message)}finally{await refresh();}};
function downloadJSON(data,filename){
 const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);
 const link=document.createElement('a');link.href=url;link.download=filename;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1500);
}
$('exportChanges').onclick=()=>{if(!current)return alert('انتظر تحميل البيانات');downloadJSON({product:'ONE COMMAND',type:'planning_backup',version:1,exportedAt:new Date().toISOString(),changes:current.changes||[]},'ONE_COMMAND_Tasks_'+new Date().toISOString().slice(0,10)+'.json')};
$('importChanges').onclick=()=>{$('importFile').value='';$('importFile').click()};
$('importFile').onchange=async()=>{
 const file=$('importFile').files?.[0];if(!file)return;
 if(file.size>80000)return alert('الملف كبير جدًا');
 if(!confirm('سيتم دمج طلبات النسخة مع الطلبات الحالية، بدون تنفيذ أي تعديل. هل توافق؟'))return;
 try{const data=JSON.parse(await file.text());const result=await request('/api/changes/import','POST',data);
    await refresh();alert('تمت استعادة '+result.imported+' طلب للمراجعة')}
 catch(e){alert('فشلت الاستعادة: '+e.message)}
};
$('restoreBrowser').onclick=async()=>{
 const saved=readBrowserBackup();
 if(!saved)return alert('لا توجد نسخة محفوظة داخل هذا المتصفح.');
 if(!confirm('هل تريد استعادة '+saved.changes.length+' طلب من المتصفح؟ لن تتغير قواعد بيانات ONE ERP.'))return;
 try{const r=await request('/api/changes/import','POST',saved);await refresh();alert('تمت استعادة '+r.imported+' طلب.')}catch(e){alert('فشل استعادة الطلبات: '+e.message)}
};
$('forgetBrowser').onclick=()=>{
 try{
  const disabled=localStorage.getItem(BACKUP_DISABLED_KEY)==='1';
  if(disabled){localStorage.removeItem(BACKUP_DISABLED_KEY);updateBrowserBackup(current?.changes||[]);return}
  if(!confirm('مسح نسخة الطلبات من هذا المتصفح وإيقاف حفظها التلقائي؟ بيانات الموقع نفسها لن تتغير.'))return;
  localStorage.removeItem(BACKUP_KEY);localStorage.setItem(BACKUP_DISABLED_KEY,'1');
  updateBrowserBackup(current?.changes||[]);
 }catch(e){alert('تعذر تعديل نسخة المتصفح: '+e.message)}
};
$('changeForm').onsubmit=async e=>{e.preventDefault();const payload={title:$('changeTitle').value.trim(),target:$('changeTarget').value,details:$('changeDetails').value.trim()};try{await request('/api/changes','POST',payload);$('changeForm').reset();val('changeNotice',' تم تسجيل الطلب للمراجعة فقط.');await refresh()}catch(error){val('changeNotice',' '+error.message)}};
$('logout').onclick=async()=>{await request('/api/logout','POST',{});location.reload()};
refresh();setInterval(refresh,6000);setInterval(()=>{if(currentPhoneId)phoneHeartbeat()},6000);