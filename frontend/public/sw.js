// PanWatch Service Worker —— 自毁版
//
// 这个应用是本地部署的实时行情/回测工具，离线缓存收益很小，却会在频繁发版后
// 让旧页面继续请求已被删除的 hash chunk，导致「页面加载失败」。
// 所以这里主动注销自己并清空所有缓存，之后不再注册新的 Service Worker。
self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
      await self.registration.unregister();
      const clients = await self.clients.matchAll({ type: 'window' });
      clients.forEach((client) => client.postMessage({ type: 'PANWATCH_SW_REMOVED' }));
    })(),
  );
});

self.addEventListener('fetch', () => {
  // 不做任何拦截，全部走网络
});
