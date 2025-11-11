self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => clients.claim());

// KEIN CACHING – alle Requests direkt ins Netz
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') { e.respondWith(fetch(e.request)); return; }
  e.respondWith(fetch(e.request));
});

// Push anzeigen (bleibt gleich)
self.addEventListener('push', e => {
  let data = {};
  try { data = e.data.json(); } catch {}
  const title = data.title || 'Autoscan';
  const body  = data.body  || '';
  const url   = data.url   || '/';
  e.waitUntil(self.registration.showNotification(title, {
    body, icon: '/static/icon-192.png', data: {url}
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data?.url || '/';
  e.waitUntil(clients.openWindow(url));
});