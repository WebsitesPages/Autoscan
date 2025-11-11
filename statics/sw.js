self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { self.clients.claim(); });

// Push-Ereignis → Notification anzeigen
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  const title = data.title || 'Autoscan Alert';
  const body  = data.body  || 'Neues Angebot';
  const url   = data.url   || '/';
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      data: { url }
    })
  );
});

// Klick auf die Notification → Link öffnen
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(win => {
      for (const c of win) {
        if ('focus' in c) { c.postMessage({type:'navigate', url}); return c.focus(); }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});