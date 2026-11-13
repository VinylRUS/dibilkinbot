/* DeeBeelkin service worker (v3.11) — офлайн-кеш статики + сетевой фолбэк.
   Консервативный: страницы всегда из сети (кеш только как офлайн-фолбэк),
   /static/ — stale-while-revalidate. API/не-GET никогда не кешируются. */
const CACHE = 'deebeelkin-v1';
const PRECACHE = [
  '/static/style.css',
  '/static/bee.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
];
const OFFLINE_HTML =
  '<!doctype html><html lang="ru"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<title>Офлайн</title><style>body{margin:0;min-height:100vh;display:flex;' +
  'align-items:center;justify-content:center;background:#0F0F1A;color:#fff;' +
  'font-family:-apple-system,Segoe UI,Roboto,sans-serif;text-align:center}' +
  'div{padding:24px}h1{color:#FFB703}</style></head><body><div>' +
  '<h1>🐝 Нет сети</h1><p>DeeBeelkin недоступен офлайн. Проверьте подключение.</p>' +
  '</div></body></html>';

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Страницы: сеть, при офлайне — офлайн-заглушка
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).catch(() =>
        new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })
      )
    );
    return;
  }

  // Статика: stale-while-revalidate
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req)
            .then((resp) => {
              if (resp && resp.status === 200) cache.put(req, resp.clone());
              return resp;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
  }
});
