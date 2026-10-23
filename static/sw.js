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
    for (const c of list) if (c.url.includes("/")) return c.focus();
    return clients.openWindow("/");
  }));
});
