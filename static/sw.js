/* DC Panel Web Push Service Worker */
self.addEventListener("push", (event) => {
  let data = { title: "DC Panel", body: "容器更新通知" };
  try {
    const d = event.data ? event.data.json() : {};
    if (d.body) { data = d; }
    else if (event.data) { data.body = event.data.text(); }
  } catch (e) {
    if (event.data) data.body = event.data.text();
  }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: "/static/icon-192.png",
    badge: "/static/favicon-32.png",
    image: undefined,
    tag: "dc-panel-" + Date.now(),
    renotify: true,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (c.url.includes("/")) {
        // iOS PWA 会保留后台窗口：直接 focus 只会恢复旧 DOM（页面不重载，
        // 用户看到的是缓存里的旧 UI）。先通知页面重载，再 focus。
        c.postMessage({ type: "dcp-reload", src: "notificationclick" });
        return c.focus();
      }
    }
    // 没有已开窗口：新开一个（带 cache-bust，确保拿到最新页面）
    return clients.openWindow("/?t=" + Date.now());
  }));
});
