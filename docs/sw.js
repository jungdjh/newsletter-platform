const CACHE='studio-static-v2';
const SHELL=['./','./index.html','./manifest.webmanifest','./api/sample.json','./icons/icon-192.png','./icons/apple-touch-icon.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()));});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==CACHE).map(x=>caches.delete(x)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  // Navigations = network-first: a shipped page fix can never be pinned by a
  // stale cache again. Fall back to cache (then index.html) only when offline.
  if(e.request.mode==='navigate'){
    e.respondWith(fetch(e.request).catch(()=>caches.match(e.request).then(h=>h||caches.match('./index.html'))));
    return;
  }
  // Other assets = cache-first for speed/offline; the CACHE bump refreshes them.
  e.respondWith(caches.match(e.request).then(h=>h||fetch(e.request)));
});
