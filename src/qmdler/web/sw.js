// SPDX-License-Identifier: GPL-3.0-or-later
// Service Worker：只缓存应用外壳（离线也能打开界面），API 与 WS 一律走网络。
//
// 下载任务是有状态的实时操作，缓存 API 响应会让 UI 显示过时状态，所以只对
// 静态资源做 cache-first。

const CACHE = 'qmdler-shell-v1';
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/css/tokens.css',
  '/css/layout.css',
  '/css/mobile.css',
  '/vendor/vue.global.prod.js',
  '/vendor/sortable.min.js',
  '/js/app.js',
  '/js/api.js',
  '/js/ws.js',
  '/js/store.js',
  '/js/views/login.js',
  '/js/views/sources.js',
  '/js/views/tracks.js',
  '/js/views/tasks.js',
  '/js/views/settings.js',
  '/js/components/quality-chain.js',
  '/icons/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});
