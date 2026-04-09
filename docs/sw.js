const CACHE = 'coupon-king-v1';
const STATIC = ['/index.html', '/style.css', '/app.js', '/manifest.json'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(STATIC))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  // Only cache same-origin static assets; let API calls (cross-origin) pass through
  if (!event.request.url.startsWith(self.location.origin)) return;

  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
