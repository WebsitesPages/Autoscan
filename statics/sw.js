// /static/sw.js
const CACHE_VERSION = 'v3'; // hochzählen
self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE_VERSION).then(c => c.addAll([
    '/', '/static/manifest.webmanifest'
  ])));
});
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', (e) => {
  e.respondWith((async () => {
    try { return await fetch(e.request); } catch { 
      const c = await caches.open(CACHE_VERSION);
      const m = await c.match(e.request, {ignoreSearch:true});
      return m || Response.error();
    }
  })());
});