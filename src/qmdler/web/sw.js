// SPDX-License-Identifier: GPL-3.0-or-later
// Service Worker：只缓存应用外壳（离线也能打开界面），API 与 WS 一律走网络。
//
// 下载任务是有状态的实时操作，缓存 API 响应会让 UI 显示过时状态，所以只对
// 静态资源做缓存。
//
// **外壳一律 network-first，不能 cache-first。** 原来对 JS/CSS 走的是
// `return cached || network`：只要缓存里有就直接返回，网络那份即使更新了也
// 要等下一次访问才生效。而缓存名是写死的 `qmdler-shell-v1`，sw.js 本身在
// 版本更新时内容不变 → 浏览器认为 SW 没变 → 不会重新 install → 旧缓存永久
// 有效。实测结果是升级后前端代码怎么改都不生效，只有手动清站点数据才行
// （本次测试里就撞上了：改完 settings.js 反复验证都还是旧行为）。
//
// 现在的策略：外壳走 network-first、拿到就顺手更新缓存，网络失败才回退到
// 缓存 —— 离线可用这条保住了，同时不会再把用户钉死在旧版本上。
// vendor/ 下的第三方库带版本号且不会原地改动，继续走 cache-first。

// 改缓存策略时必须同时改这个名字：activate 阶段会删掉所有别的缓存，
// 老客户端升级上来时才能把旧的 cache-first 内容清干净。
const CACHE = 'qmdler-shell-v2';
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

  // 第三方库带版本号、不会原地改动，走 cache-first 省一次请求。
  const immutable = url.pathname.startsWith('/vendor/') || url.pathname.startsWith('/icons/');

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (immutable && cached) return cached;

      return fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => {
          // 只有网络真的不可用时才吃缓存，这样离线仍能打开界面。
          if (cached) return cached;
          throw new Error('offline and not cached');
        });
    }),
  );
});
