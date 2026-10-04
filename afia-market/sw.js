const C="afia-market-v16";
const A=["./","./index.html","./admin.html","./manifest.webmanifest","./icon.svg"];
self.addEventListener("install",e=>{
  self.skipWaiting();
  e.waitUntil(caches.open(C).then(c=>c.addAll(A)));
});
self.addEventListener("activate",e=>e.waitUntil(Promise.all([
  clients.claim(),
  caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==C).map(k=>caches.delete(k))))
])));
self.addEventListener("fetch",e=>{
  const u=new URL(e.request.url);
  if(u.origin!==self.location.origin)return;
  if(e.request.mode==="navigate"){
    e.respondWith(
      fetch(e.request,{cache:"no-store"}).then(r=>{
        const x=r.clone();
        caches.open(C).then(c=>c.put(e.request,x));
        return r;
      }).catch(()=>caches.match(e.request))
    );
    return;
  }
  e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request,{cache:"no-store"})));
});