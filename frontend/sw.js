/* Only the public, self-contained offline page is cached. No private data,
   authentication, HTML shell, application JS, photos, or Excel downloads. */
'use strict';
const CACHE_PREFIX = 'myraa-pwa-offline-';
const CACHE = CACHE_PREFIX + 'nov25-r18';
const OFFLINE = '/app/offline.html';
const NAVIGATION_PATHS = new Set(['/app/', '/app/index.html', '/app/sign-in.html']);
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const response = await fetch(OFFLINE, {cache:'no-store', credentials:'omit', redirect:'error'});
    if (!response.ok || !response.headers.get('content-type')?.includes('text/html')) throw new Error('Offline page unavailable');
    const cache = await caches.open(CACHE);
    await cache.put(OFFLINE, response);
    await self.skipWaiting();
  })());
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith(CACHE_PREFIX) && key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || request.mode !== 'navigate' ||
      url.origin !== self.location.origin || !NAVIGATION_PATHS.has(url.pathname)) return;
  event.respondWith((async () => {
    try { return await fetch(request, {cache:'no-store'}); }
    catch {
      const cache = await caches.open(CACHE);
      return await cache.match(OFFLINE) || new Response('MYRAA is offline. Reconnect and reload.', {status:503, headers:{'Content-Type':'text/plain; charset=utf-8'}});
    }
  })());
});
