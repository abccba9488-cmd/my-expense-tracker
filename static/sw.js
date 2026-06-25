// PWA service worker: cache app shell assets, always fetch fresh data for API/HTML.
const CACHE_NAME = 'bao-shell-v4';
// app.js / style.css are intentionally NOT precached here — index.html now
// references them through a mtime-versioned query string (?v=...), so the
// exact URL changes on every deploy and is always a fresh network fetch the
// first time; precaching the bare unversioned URL here would just be dead
// weight no request ever matches.
const SHELL_ASSETS = [
  '/static/img/icons/icon-192.png',
  '/static/img/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept API calls — data must always be fresh.
  if (url.pathname.startsWith('/api/')) return;

  // Cache-first for static assets, fall back to network and refresh cache.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const fetchPromise = fetch(event.request).then((response) => {
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, response.clone()));
          return response;
        });
        return cached || fetchPromise;
      })
    );
  }
});
