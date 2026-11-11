/* Only the application shell is cached. API, costs, tokens and financial data
   never enter CacheStorage or a background fetch. */
const VERSION = 'varanez-shell-v5';
const ROOT = new URL('./', self.location.href).pathname;
const FILES = ['', 'index.html', 'app.css', 'app.mjs', 'model.mjs', 'storage.mjs',
  'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];
const ALLOWED = new Set(FILES.map(file => ROOT + file));
self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(FILES.map(file => ROOT + file))));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('varanez-shell-') && key !== VERSION).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('message', event => { if (event.data === 'ACTIVATE') self.skipWaiting(); });
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.search || !ALLOWED.has(url.pathname)) return;
  // All shell files belong to one installed version, preventing mixed modules.
  event.respondWith(caches.open(VERSION).then(async cache => (await cache.match(event.request)) || fetch(event.request)));
});
