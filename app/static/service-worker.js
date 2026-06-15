const CACHE_NAME = 'invsync-v3';
const URLS_TO_CACHE = ['/', '/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS_TO_CACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }

  const req = event.request;
  const url = new URL(req.url);
  const isSameOrigin = url.origin === self.location.origin;
  const isStaticAsset =
    url.pathname.startsWith('/static/') ||
    url.pathname === '/manifest.json' ||
    url.pathname.startsWith('/api/documents/image/');
  const accept = req.headers.get('accept') || '';
  const isHtmlNavigation = req.mode === 'navigate' || accept.includes('text/html');

  // Never cache API responses; always fetch fresh data.
  if (isSameOrigin && url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(req, { cache: 'no-store' }));
    return;
  }

  if (isSameOrigin && isHtmlNavigation) {
    event.respondWith(
      fetch(req)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return response;
        })
        .catch(() => caches.match(req).then((cached) => cached || caches.match('/')))
    );
    return;
  }

  if (!isSameOrigin) {
    return;
  }

  // For same-origin non-static requests, skip cache and go to network.
  if (!isStaticAsset) {
    event.respondWith(fetch(req));
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) {
        return cached;
      }
      return fetch(req)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return response;
        })
        .catch(() => caches.match('/'));
    })
  );
});
